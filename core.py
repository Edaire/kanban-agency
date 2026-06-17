"""kanban-agency core scan/dry-run logic."""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import re
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

try:
    from hermes_cli import kanban_db as kb  # type: ignore
except Exception:  # pragma: no cover - local checkout fallback
    REPO_ROOT = Path(__file__).resolve().parents[3] / "code" / "opensource" / "hermes-agent"
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from hermes_cli import kanban_db as kb  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path.home() / ".hermes" / "kanban-agency" / "roles.yaml"
TTYD_WHEEL_INDEX = PLUGIN_DIR / "assets" / "ttyd-wheel-index.html"
VALID_PROVIDERS = {"codex", "claude", "hermes", "human"}
ROLE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
WORKDIR_RE = re.compile(r"(?m)^\s*workdir:\s*(\S.*?)\s*$")
ACTIVE_STATUSES = {"todo", "ready", "running", "blocked", "review"}
SKIP_STATUSES = {"done", "archived"}
INDEPENDENT_ROLE_BOARD = "kanban_agency_independent_tasks"
INDEPENDENT_ROLE_BOARD_NAME = "Independent Role Chats"
INDEPENDENT_ROLE_DEFAULT_WORKDIR = str(Path.home() / "code" / "edd" / "mcps")
DEFAULT_INDEPENDENT_ROLES = ["orchestrator", "researcher", "analyst", "architect", "developer", "tester", "ops", "assistant"]
ROLE_DESCRIPTIONS = {
    "orchestrator": "拆解目标、协调角色、推进多步骤流程",
    "researcher": "资料检索、背景调研、方案对比",
    "analyst": "需求澄清、范围定义、验收口径",
    "architect": "技术方案、任务拆分、风险识别",
    "developer": "实现功能、修复缺陷、提交前自测",
    "tester": "验证实现、回归测试、smoke 与结论",
    "ops": "排障、日志、服务状态与运维操作",
    "assistant": "通用助手、临时问题、轻量协作",
}
ROLE_WORKSPACE_DIR = Path.home() / ".hermes" / "kanban-agency" / "role-workspaces"


def _kanban_management_module():
    spec = importlib.util.spec_from_file_location("kanban_agency_kanban_management", PLUGIN_DIR / "kanban_management.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load kanban_management module")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


@dataclass
class Role:
    key: str
    provider: str
    rules: list[str]
    aliases: list[str]
    title: str = ""
    description: str = ""


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def load_roles(path: Path = CONFIG_PATH) -> tuple[OrderedDict[str, Role], list[str]]:
    warnings: list[str] = []
    if yaml is None:
        raise ValueError("PyYAML is required to read roles.yaml")
    if not path.exists():
        raise ValueError(f"roles.yaml not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "roles" not in data:
        raise ValueError("roles.yaml must contain top-level 'roles'")
    raw_roles = data["roles"]
    if not isinstance(raw_roles, dict):
        raise ValueError("roles must be a mapping")
    if "default" not in raw_roles:
        raise ValueError("roles.default is required")

    roles: OrderedDict[str, Role] = OrderedDict()
    for key, raw in raw_roles.items():
        if not isinstance(key, str) or not ROLE_KEY_RE.match(key):
            raise ValueError(f"invalid role key: {key!r}")
        if not isinstance(raw, dict):
            raise ValueError(f"role {key} must be a mapping")
        provider = raw.get("provider")
        if provider not in VALID_PROVIDERS:
            raise ValueError(f"role {key} has invalid provider: {provider!r}")
        rules_raw = raw.get("rules", [])
        aliases_raw = raw.get("aliases", [])
        if rules_raw is None:
            rules_raw = []
        if aliases_raw is None:
            aliases_raw = []
        if not isinstance(rules_raw, list) or not all(isinstance(x, str) for x in rules_raw):
            raise ValueError(f"role {key}.rules must be a string array")
        if not isinstance(aliases_raw, list) or not all(isinstance(x, str) for x in aliases_raw):
            raise ValueError(f"role {key}.aliases must be a string array")
        title = str(raw.get("title") or key)
        description = str(raw.get("description") or "")
        roles[key] = Role(key=key, provider=str(provider), rules=list(rules_raw), aliases=list(aliases_raw), title=title, description=description)
    return roles, warnings


def _resolve_workdir(task_body: str, board: str) -> tuple[str | None, list[str]]:
    warnings: list[str] = []
    matches = [m.group(1).strip() for m in WORKDIR_RE.finditer(task_body or "")]
    if matches:
        if len(matches) > 1:
            warnings.append("multiple workdir lines found; using first")
        raw = matches[0]
        if not raw.startswith("/"):
            warnings.append(f"invalid non-absolute workdir ignored: {raw}")
        else:
            p = Path(raw)
            if not p.exists():
                warnings.append(f"workdir does not exist: {raw}")
            return raw, warnings
    meta = kb.read_board_metadata(board)
    default = meta.get("default_workdir")
    if default:
        raw = str(default)
        if raw.startswith("/"):
            if not Path(raw).exists():
                warnings.append(f"board default_workdir does not exist: {raw}")
            return raw, warnings
        warnings.append(f"invalid board default_workdir ignored: {raw}")
    warnings.append("no workdir found; relative rules cannot be resolved")
    return None, warnings


def _rule_warnings(role: Role, workdir: str | None) -> list[str]:
    warnings: list[str] = []
    for rule in role.rules:
        p = Path(rule)
        if not p.is_absolute():
            if not workdir:
                warnings.append(f"rule cannot be resolved without workdir: {role.key}:{rule}")
                continue
            p = Path(workdir) / rule
        if not p.exists():
            warnings.append(f"rule file not found: {role.key}:{p}")
    return warnings


def _dedup_aliases(role: Role) -> list[str]:
    seen = set()
    out = []
    for alias in [role.key, *role.aliases]:
        a = alias.strip()
        if not a:
            continue
        folded = a.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        out.append(a)
    return out


def match_roles(title: str, body: str, roles: OrderedDict[str, Role]) -> tuple[list[str], str, list[dict[str, str]]]:
    haystack = f"{title or ''}\n{body or ''}".casefold()
    matched: list[str] = []
    reasons: list[dict[str, str]] = []
    default_explicit = False
    default_reason: dict[str, str] | None = None
    for key, role in roles.items():
        for alias in _dedup_aliases(role):
            if alias.casefold() in haystack:
                reason = {"role": key, "alias": alias, "source": "title+body"}
                if key == "default":
                    default_explicit = True
                    default_reason = reason
                else:
                    matched.append(key)
                    reasons.append(reason)
                break
    if len(matched) == 1:
        return matched, matched[0], reasons
    if len(matched) > 1:
        return matched, "default", reasons
    if default_explicit and default_reason is not None:
        return ["default"], "default", [default_reason]
    return [], "default", []


def _task_rows(conn) -> list[Any]:
    rows = conn.execute("SELECT * FROM tasks ORDER BY created_at, id").fetchall()
    return rows


def _children(conn, root_id: str) -> list[Any]:
    return conn.execute(
        "SELECT t.* FROM task_links l JOIN tasks t ON t.id = l.child_id WHERE l.parent_id=? ORDER BY t.created_at, t.id",
        (root_id,),
    ).fetchall()


def _find_role_child(conn, root_id: str, role: str) -> str | None:
    prefix = f"[agency] {role}:"
    for row in _children(conn, root_id):
        title = row["title"] or ""
        if title.startswith(prefix):
            return str(row["id"])
    return None


def _role_body(root: dict[str, Any], role: Role) -> str:
    rules = "\n".join(f"- {r}" for r in role.rules) or "- (none)"
    root_body = root.get("body") or ""
    return (
        "@kanban-agency-role\n"
        f"root_id: {root['root_id']}\n"
        f"role: {role.key}\n"
        f"provider: {role.provider}\n"
        f"workdir: {root.get('workdir') or ''}\n"
        f"root_title: {root.get('title') or ''}\n\n"
        "rules:\n"
        f"{rules}\n\n"
        "root_task_body:\n"
        "```text\n"
        f"{root_body}\n"
        "```\n\n"
        "This is a kanban-agency role card. It represents a provider-backed role session. "
        "The root_task_body above is the concrete active task; provider session startup/continuation is managed by kanban-agency run/continue.\n"
    )


def start(board: str, roles_path: Path = CONFIG_PATH) -> dict[str, Any]:
    scanned = scan(board, roles_path)
    if scanned.get("errors"):
        return {"board": board, "created": [], "reused": [], "skipped": [], "errors": scanned["errors"], "scan": scanned}
    try:
        roles, _ = load_roles(roles_path)
    except Exception as exc:
        return {"board": board, "created": [], "reused": [], "skipped": [], "errors": [str(exc)], "scan": scanned}
    created: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    conn = kb.connect(board=board)
    try:
        for root in scanned.get("roots", []):
            role_key = root.get("route_to") or "default"
            role = roles.get(role_key)
            if role is None:
                errors.append(f"unknown route_to role for {root.get('root_id')}: {role_key}")
                continue
            existing = _find_role_child(conn, root["root_id"], role_key)
            if existing:
                reused.append({"root_id": root["root_id"], "role": role_key, "task_id": existing})
                continue
            title = f"[agency] {role_key}: {root.get('title') or root['root_id']}"
            try:
                task_id = kb.create_task(
                    conn,
                    title=title,
                    body=_role_body(root, role),
                    assignee=_agency_assignee(role_key),
                    created_by="kanban-agency",
                    workspace_kind="dir" if root.get("workdir") else "scratch",
                    workspace_path=root.get("workdir"),
                    parents=[root["root_id"]],
                    initial_status="running",
                )
                try:
                    kb.promote_task(conn, task_id, actor="kanban-agency", reason="root is aggregate anchor, role card should be active", force=True)
                except Exception as exc:
                    errors.append(f"created {task_id} but promote failed: {exc}")
                created.append({"root_id": root["root_id"], "role": role_key, "task_id": task_id, "title": title, "provider": role.provider})
            except Exception as exc:
                skipped.append({"root_id": root.get("root_id"), "role": role_key, "reason": str(exc)})
    finally:
        conn.close()
    return {"board": board, "created": created, "reused": reused, "skipped": skipped, "errors": errors, "scan": scanned}


def _parse_role_body(body: str | None) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in (body or "").splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        if key in {"root_id", "role", "provider", "workdir", "root_title", "provider_session_ref"}:
            data[key] = val.strip()
    return data



def _agency_assignee(role: str | None) -> str | None:
    role = (role or "").strip().lower()
    return f"agency-{role}" if role else None

def _parse_role_rules(body: str | None) -> list[str]:
    rules: list[str] = []
    in_rules = False
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped == "rules:":
            in_rules = True
            continue
        if in_rules:
            if not stripped:
                break
            if stripped.startswith("- "):
                rule = stripped[2:].strip()
                if rule and rule != "(none)":
                    rules.append(rule)
            else:
                break
    return rules




def _session_cwd_compatible(state: dict[str, Any], desired_cwd: str | None) -> bool:
    desired = (desired_cwd or "").strip()
    recorded = str(state.get("cwd") or "").strip()
    if not desired or not recorded:
        return True
    try:
        return str(Path(recorded).expanduser().resolve()) == str(Path(desired).expanduser().resolve())
    except Exception:
        return recorded == desired

def _resolve_codex_session_ref(conn, task: kb.Task, meta: dict[str, str]) -> dict[str, Any]:
    """Resolve the Codex thread to use for this task.

    Task identity remains separate from provider session identity. A task may
    declare provider_session_ref: <task_id> to reuse another task's Codex thread.
    If absent, default to an existing task under the same root+role that already
    has a thread_id. Results/comments still belong to the current task.
    """
    current_state = _load_bridge_state(task.id)
    desired_cwd = meta.get("workdir") or getattr(task, "workspace_path", None)
    reuse_flag = (meta.get("session_reuse") or meta.get("reuse_session") or "true").strip().lower()
    if reuse_flag in {"false", "0", "no", "new"}:
        if current_state.get("thread_id") and _session_cwd_compatible(current_state, desired_cwd):
            return {"thread_id": current_state.get("thread_id"), "session_task_id": task.id, "source": "self"}
        return {"thread_id": None, "session_task_id": task.id, "source": "session_reuse_disabled"}
    if current_state.get("thread_id") and _session_cwd_compatible(current_state, desired_cwd):
        return {"thread_id": current_state.get("thread_id"), "session_task_id": task.id, "source": "self"}
    ref = (meta.get("provider_session_ref") or "").strip()
    if ref:
        ref_state = _load_bridge_state(ref)
        if ref_state.get("thread_id") and _session_cwd_compatible(ref_state, desired_cwd):
            return {"thread_id": ref_state.get("thread_id"), "session_task_id": ref, "source": "provider_session_ref"}
    root_id = (meta.get("root_id") or "").strip()
    role = (meta.get("role") or "").strip()
    if root_id and role:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE id != ? AND title LIKE '[agency] %' AND body LIKE ? ORDER BY created_at, id",
            (task.id, f"%root_id: {root_id}%"),
        ).fetchall()
        for row in rows:
            other = kb.Task.from_row(row)
            other_meta = _parse_role_body(other.body)
            if other_meta.get("role") != role:
                continue
            other_state = _load_bridge_state(other.id)
            if other_state.get("thread_id") and _session_cwd_compatible(other_state, desired_cwd):
                return {"thread_id": other_state.get("thread_id"), "session_task_id": other.id, "source": "same_root_role"}
    return {"thread_id": None, "session_task_id": task.id, "source": "new"}

def _load_codex_runner():
    plugin = Path.home() / ".hermes/plugins/codex-kanban-runner/__init__.py"
    if not plugin.exists():
        raise RuntimeError(f"codex-kanban-runner plugin not found: {plugin}")
    spec = importlib.util.spec_from_file_location("codex_kanban_runner_for_agency", plugin)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _role_rows(conn, task_id: str | None = None) -> list[Any]:
    if task_id:
        return conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND title LIKE '[agency] %' AND status NOT IN ('done','archived') ORDER BY created_at, id",
            (task_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM tasks WHERE title LIKE '[agency] %' AND status NOT IN ('done','archived') ORDER BY created_at, id"
    ).fetchall()


def _codex_prompt(task: kb.Task, meta: dict[str, str]) -> str:
    """Minimal native Codex prompt for a role task.

    Do not inline rule files or the whole role card. The native Codex session can
    read the rule paths from disk, which keeps startup prompts small and avoids
    swamping the model with boilerplate. For independent tasks, include the
    user-provided root_task_body so title + description both reach the agent.
    """
    role = meta.get('role', 'unknown')
    root_title = meta.get('root_title') or task.title or ''
    task_body = _extract_root_task_body(task.body or '').strip()
    workdir = meta.get('workdir') or ''
    rules = _parse_role_rules(task.body)
    rules_text = "\n".join(f"- {r}" for r in rules) or "- (none)"
    role_names = {
        "analyst": "需求分析师",
        "architect": "架构设计师",
        "developer": "开发工程师",
        "tester": "测试工程师",
        "assistant": "通用执行助手",
        "ops": "运维工程师",
        "operator": "运维工程师",
    }
    details = ""
    if task_body and task_body != root_title:
        details = f"任务描述：\n{task_body}\n\n"
    if role in {"ops", "operator"}:
        action_line = f"需要做的运维任务是：{root_title}\n\n"
        tail = "请按工作规则阅读必要文件，然后执行运维检查/操作。遇到登录、审批、外部系统权限或高风险操作时停下来等用户处理。完成后停止，等待用户在看板点击 Complete。\n"
    elif role == "assistant":
        action_line = f"需要做的任务是：{root_title}\n\n"
        tail = "请按工作规则做最小必要操作，不要主动扩大范围。需要用户确认、权限或上下文时停下来提问。完成后停止，等待用户在看板点击 Complete。\n"
    else:
        action_line = f"需要做的功能是：{root_title}\n\n"
        tail = "请按工作规则阅读必要文件，然后只完成当前角色职责。需要用户补充信息或审批时就停下来明确提问。完成当前角色工作后停止，等待用户在看板点击 Complete。\n"
    return (
        f"你是一个{role_names.get(role, role)}。\n"
        f"工作目录：{workdir}\n"
        f"工作规则在：\n{rules_text}\n\n"
        f"{action_line}"
        f"{details}"
        f"{tail}"
    )


def _hermes_prompt(task: kb.Task, meta: dict[str, str]) -> str:
    role = meta.get('role', 'unknown')
    root_title = meta.get('root_title') or task.title or ''
    workdir = meta.get('workdir') or ''
    rules = _parse_role_rules(task.body)
    rules_text = "\n".join(f"- {r}" for r in rules) or "- (none)"
    return (
        f"你是 kanban-agency 的 {role} 角色。\n"
        f"工作目录：{workdir}\n"
        f"工作规则在：\n{rules_text}\n\n"
        f"当前任务：{root_title}\n\n"
        "请只完成当前角色职责。需要用户补充信息或审批时停下来提问；完成后停止，等待用户在看板点击 Complete。\n"
    )

CLAUDE_RUN_DIR = Path.home() / ".hermes" / "kanban-agency" / "claude-runs"

def _claude_state_path(task_id: str) -> Path:
    return CLAUDE_RUN_DIR / task_id / "state.json"

def _read_claude_state(task_id: str) -> dict[str, Any]:
    return _read_json_file(_claude_state_path(task_id))


def _extract_root_task_body(role_body: str) -> str:
    marker = "root_task_body:\n```text\n"
    if marker not in role_body:
        return role_body
    rest = role_body.split(marker, 1)[1]
    return rest.split("\n```", 1)[0].strip()

def _claude_prompt(task: kb.Task, meta: dict[str, str]) -> str:
    """Minimal native Claude prompt for ops-style sessions."""
    body = _extract_root_task_body(task.body or '').strip() or (meta.get('root_title') or task.title or '')
    workdir = meta.get("workdir") or ""
    return (
        "你是一个运维工程师。\n"
        f"工作目录：{workdir}\n"
        "工作规则在：.kiro/steering 中与当前任务相关的文档。\n\n"
        f"需要做的运维任务是：{body}\n\n"
        "请先阅读相关规则/文档，再执行运维检查或操作。"
        "遇到登录、MCP 权限、审批、外部系统权限或高风险操作时，停在这个 Claude TUI 中等待用户处理。"
        "完成后停止，等待用户在看板点击 Complete。\n"
    )

def _ensure_claude_ops_settings() -> Path:
    """Build a minimal Claude settings file for ops sessions.

    It keeps MCP servers + explicit permissions, but avoids hooks/plugins that can
    hijack kanban-agency role tasks.
    """
    dest = Path.home() / ".hermes" / "kanban-agency" / "claude-ops-settings.json"
    try:
        src = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        src = {}
    data = {
        "env": src.get("env", {}),
        "model": src.get("model"),
        "mcpServers": src.get("mcpServers", {}),
        "permissions": src.get("permissions", {}),
        "hasCompletedOnboarding": True,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest

def _tmux_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    return env


_TMUX_SESSIONS_CACHE: tuple[float, set[str]] = (0.0, set())
_TMUX_SESSIONS_CACHE_TTL = 0.75


def _tmux_session_names() -> set[str]:
    global _TMUX_SESSIONS_CACHE
    now = time.time()
    ts, names = _TMUX_SESSIONS_CACHE
    if now - ts < _TMUX_SESSIONS_CACHE_TTL:
        return names
    try:
        cp = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            text=True,
            capture_output=True,
            timeout=2,
            env=_tmux_env(),
        )
        names = set((cp.stdout or "").splitlines()) if cp.returncode == 0 else set()
    except Exception:
        names = set()
    _TMUX_SESSIONS_CACHE = (now, names)
    return names


def _tmux_has_session(name: str) -> bool:
    names = _tmux_session_names()
    if names:
        return name in names
    try:
        cp = subprocess.run(["tmux", "has-session", "-t", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_tmux_env())
        return cp.returncode == 0
    except Exception:
        return False



def _prompt_still_visible(screen: str, prompt_path: Path) -> bool:
    try:
        prompt = prompt_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        prompt = ""
    markers = ["工作目录：", "需要做的功能是：", "需要做的任务是：", "请按工作规则"]
    return sum(1 for m in markers if m in screen) >= 2


def _prompt_missing_from_input(screen: str, prompt_path: Path) -> bool:
    """Return True when Codex is idle but the intended prompt is not in input.

    A too-early tmux paste can be dropped while the native TUI is still
    starting. In that case the pane shows the default placeholder (for example
    "Find and fix a bug in @filename") and the old _prompt_still_visible()
    check incorrectly considered the prompt submitted because the markers were
    absent. Treat that as a missing paste so callers can retry.
    """
    if _prompt_still_visible(screen, prompt_path):
        return False
    idle_markers = ["Find and fix a bug in @filename", "› ", ">_"]
    prompt_markers = ["工作目录：", "需要做的功能是：", "需要做的任务是：", "请按工作规则", "@kanban-agency"]
    return any(m in screen for m in idle_markers) and not any(m in screen for m in prompt_markers)


def _tmux_capture(tmux_name: str, lines: int = 80) -> str:
    return subprocess.check_output(
        ["tmux", "capture-pane", "-t", str(tmux_name), "-p", "-S", f"-{int(lines)}"],
        text=True,
        stderr=subprocess.STDOUT,
        env=_tmux_env(),
    )


def _paste_prompt(tmux_name: str, prompt_path: Path, submit: bool) -> None:
    subprocess.run(["tmux", "load-buffer", "-t", str(tmux_name), str(prompt_path)], check=False, env=_tmux_env())
    subprocess.run(["tmux", "paste-buffer", "-t", str(tmux_name)], check=False, env=_tmux_env())
    if submit:
        subprocess.run(["tmux", "send-keys", "-t", str(tmux_name), "Enter"], check=False, env=_tmux_env())


def _wait_for_tui_ready(tmux_name: str, timeout: float = 8.0) -> str:
    deadline = time.time() + max(0.5, timeout)
    last = ""
    while time.time() < deadline:
        try:
            last = _tmux_capture(tmux_name, lines=40)
        except Exception:
            last = ""
        if "OpenAI Codex" in last or "› " in last or ">_" in last:
            return last
        time.sleep(0.25)
    return last


def _ensure_prompt_submitted(tmux_name: str, prompt_path: Path, attempts: int = 3, delay: float = 1.0) -> dict[str, Any]:
    """Verify pasted role prompt is not still sitting in the native TUI input box.

    Codex/Claude native TUIs occasionally need an extra Enter after tmux paste.
    If the captured pane still shows the full role prompt markers, send Enter
    again instead of leaving the Kanban task misleadingly running.
    """
    extra = 0
    last = ""
    for i in range(max(1, attempts)):
        if delay:
            time.sleep(delay)
        try:
            last = _tmux_capture(str(tmux_name), lines=80)
        except Exception as exc:
            return {"submitted": False, "extra_enter_sent": extra, "reason": f"capture_failed: {exc}"}
        if _prompt_still_visible(last, prompt_path):
            subprocess.run(["tmux", "send-keys", "-t", str(tmux_name), "Enter"], check=False, env=_tmux_env())
            extra += 1
            continue
        if _prompt_missing_from_input(last, prompt_path):
            _paste_prompt(str(tmux_name), prompt_path, submit=True)
            extra += 1
            continue
        return {"submitted": True, "extra_enter_sent": extra}
    return {"submitted": not _prompt_still_visible(last, prompt_path), "extra_enter_sent": extra, "reason": "prompt_still_visible" if _prompt_still_visible(last, prompt_path) else None}

def claude_interactive_run_task(board: str, task: kb.Task, meta: dict[str, str]) -> dict[str, Any]:
    """Start/attach a persistent native Claude TUI via tmux + ttyd.

    This is the collaboration mode: Claude stays alive in tmux, the browser
    attaches to it, and closing the page does not kill the session.
    """
    if not shutil.which("claude"):
        return {"ok": False, "error": "claude command not found"}
    if not shutil.which("ttyd"):
        return {"ok": False, "error": "ttyd command not found"}
    if not shutil.which("tmux"):
        return {"ok": False, "error": "tmux command not found"}
    state = _read_claude_state(task.id)
    existing_web = _read_json_file(_claude_web_state_path(task.id))
    existing_tmux = existing_web.get("tmux") or existing_web.get("tmux_name")
    existing_ttyd_ok = bool(existing_web.get("pid") and _pid_alive(existing_web.get("pid")) and existing_web.get("url") and _url_ok(str(existing_web.get("url"))))
    if existing_ttyd_ok and (not existing_tmux or _tmux_has_session(str(existing_tmux))):
        return {"ok": True, "reused": True, "state": state or existing_web, "url": existing_web.get("url")}
    if existing_ttyd_ok and existing_tmux and not _tmux_has_session(str(existing_tmux)):
        try:
            os.kill(int(existing_web.get("pid")), signal.SIGTERM)
        except Exception:
            pass
    cwd = Path(meta.get("workdir") or task.workspace_path or os.getcwd()).expanduser()
    cwd.mkdir(parents=True, exist_ok=True)
    d = CLAUDE_RUN_DIR / task.id; d.mkdir(parents=True, exist_ok=True)
    prompt_path = d / "prompt.md"; prompt_path.write_text(_claude_prompt(task, meta), encoding="utf-8")
    tmux_name = f"kanban-claude-{task.id}"
    if not _tmux_has_session(tmux_name):
        settings_path = _ensure_claude_ops_settings()
        claude_cmd = f"exec claude --settings {shlex.quote(str(settings_path))}"
        subprocess.run(["tmux", "new-session", "-d", "-s", tmux_name, "-c", str(cwd), "bash", "-lc", claude_cmd], check=True)
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "-g", "history-limit", "50000"], check=False)
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "-g", "mouse", "off"], check=False)
        time.sleep(1.0)
        # Paste the active task prompt into the interactive TUI and submit it.
        subprocess.run(["tmux", "load-buffer", "-t", tmux_name, str(prompt_path)], check=False)
        subprocess.run(["tmux", "paste-buffer", "-t", tmux_name], check=False)
        subprocess.run(["tmux", "send-keys", "-t", tmux_name, "Enter"], check=False)
        _ensure_prompt_submitted(str(tmux_name), prompt_path)
    CLAUDE_WEB_DIR.mkdir(parents=True, exist_ok=True)
    use_port = _free_port()
    url = f"http://127.0.0.1:{use_port}/"
    stdout_path = CLAUDE_WEB_DIR / f"{task.id}.stdout.log"
    stderr_path = CLAUDE_WEB_DIR / f"{task.id}.stderr.log"
    subprocess.run(["tmux", "set-option", "-t", tmux_name, "-g", "history-limit", "50000"], check=False)
    subprocess.run(["tmux", "set-option", "-t", tmux_name, "-g", "mouse", "off"], check=False)
    cmd = ["ttyd", "--interface", "127.0.0.1", "--port", str(use_port), "--writable", "-I", str(TTYD_WHEEL_INDEX), "-t", "scrollback=50000", "tmux", "attach-session", "-t", tmux_name]
    out = stdout_path.open("ab"); err = stderr_path.open("ab")
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
    finally:
        out.close(); err.close()
    web_state = {"task_id": task.id, "board": board, "provider": "claude", "mode": "interactive-tmux", "tmux": tmux_name, "pid": proc.pid, "port": use_port, "url": url, "cwd": str(cwd), "prompt_path": str(prompt_path), "cmd": cmd, "stdout_log": str(stdout_path), "stderr_log": str(stderr_path), "started_at": int(time.time())}
    _write_json_file(_claude_web_state_path(task.id), web_state)
    state.update({"task_id": task.id, "board": board, "provider": "claude", "state": "blocked", "reason": "interactive_session", "cwd": str(cwd), "prompt_path": str(prompt_path), "web_url": claude_session_url(task.id), "ttyd_url": url, "tmux": tmux_name, "pid": proc.pid, "started_at": int(time.time())})
    _write_json_file(_claude_state_path(task.id), state)
    conn = kb.connect(board=board)
    try:
        _set_status(conn, task.id, "blocked", result=f"Claude interactive tmux session started: {claude_session_url(task.id)}")
        ensure_claude_session_link(conn, board, task.id, session_id=state.get("session_id") or tmux_name, cwd=str(cwd))
        kb.add_comment(conn, task.id, author="kanban-agency", body=(
            "Claude interactive tmux session started.\n"
            f"URL: {claude_session_url(task.id)}\n"
            f"Direct ttyd: {url}\n"
            f"tmux: {tmux_name}\n"
            f"cwd: {cwd}\n\n"
            "Use this TUI to handle login/approval/chat. Closing the browser page will not kill the Claude tmux session."
        ))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "reused": False, "state": state, "url": claude_session_url(task.id), "ttyd_url": url, "tmux": tmux_name}



def _latest_codex_thread_for_cwd(cwd: str, since: int) -> str | None:
    root = Path.home() / ".codex" / "sessions"
    if not root.exists():
        return None
    best: tuple[float, str] | None = None
    needle = f'"cwd":"{cwd}"'
    for fp in root.rglob("*.jsonl"):
        try:
            mt = fp.stat().st_mtime
            if mt < since - 5:
                continue
            head = fp.read_text(errors="ignore")[:3000]
            if needle not in head:
                continue
            m = re.search(r'"id":"([^"/]+)"', head)
            if m and (best is None or mt > best[0]):
                best = (mt, m.group(1))
        except Exception:
            continue
    return best[1] if best else None


def codex_native_run_task(board: str, task: kb.Task, meta: dict[str, str]) -> dict[str, Any]:
    """Start/attach the real native Codex TUI in tmux.

    This is the single execution surface for Codex roles: /s attaches to the
    same tmux session, so there is no app-server-vs-resume split.
    """
    if not shutil.which("codex"):
        return {"ok": False, "error": "codex command not found"}
    if not shutil.which("ttyd"):
        return {"ok": False, "error": "ttyd command not found"}
    if not shutil.which("tmux"):
        return {"ok": False, "error": "tmux command not found"}
    cwd = Path(meta.get("workdir") or task.workspace_path or os.getcwd()).expanduser()
    cwd.mkdir(parents=True, exist_ok=True)
    state_path = _codex_web_state_path(task.id)
    state = _read_json_file(state_path)
    tmux_name = state.get("tmux_name") or f"kanban-codex-{task.id}"
    existing_pid = state.get("pid")
    if _tmux_has_session(str(tmux_name)) and existing_pid and _pid_alive(existing_pid) and state.get("url"):
        return {"ok": True, "reused": True, "state": state, "url": state.get("url")}

    CODEX_WEB_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = Path.home() / ".hermes" / "codex-kanban-runs" / task.id / "native-tui"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = run_dir / "prompt.md"
    prompt_path.write_text(_codex_prompt(task, meta), encoding="utf-8")
    started_at = int(time.time())
    submit_info: dict[str, Any] = {"submitted": False, "reason": "reused_existing_tmux"}
    if not _tmux_has_session(str(tmux_name)):
        subprocess.run(["tmux", "new-session", "-d", "-s", str(tmux_name), "-c", str(cwd), "bash", "-lc", "exec codex"], check=True, env=_tmux_env())
        subprocess.run(["tmux", "set-option", "-t", str(tmux_name), "-g", "history-limit", "50000"], check=False, env=_tmux_env())
        subprocess.run(["tmux", "set-option", "-t", str(tmux_name), "-g", "mouse", "off"], check=False, env=_tmux_env())
        _wait_for_tui_ready(str(tmux_name))
        _paste_prompt(str(tmux_name), prompt_path, submit=True)
        submit_info = _ensure_prompt_submitted(str(tmux_name), prompt_path)
    use_port = _free_port()
    url = f"http://127.0.0.1:{use_port}/"
    readonly_port = _free_port()
    readonly_url = f"http://127.0.0.1:{readonly_port}/"
    stdout_path = CODEX_WEB_DIR / f"{task.id}.stdout.log"
    stderr_path = CODEX_WEB_DIR / f"{task.id}.stderr.log"
    cmd = ["ttyd", "--interface", "127.0.0.1", "--port", str(use_port), "--writable", "-I", str(TTYD_WHEEL_INDEX), "-t", "scrollback=50000", "--client-option", f"titleFixed=Hermes {task.id}", "tmux", "attach-session", "-t", str(tmux_name)]
    readonly_cmd = ["ttyd", "--interface", "127.0.0.1", "--port", str(readonly_port), "-I", str(TTYD_WHEEL_INDEX), "-t", "scrollback=50000", "--client-option", f"titleFixed=Hermes {task.id} readonly", "tmux", "attach-session", "-t", str(tmux_name)]
    out = stdout_path.open("ab"); err = stderr_path.open("ab")
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True, env=_tmux_env())
        readonly_proc = subprocess.Popen(readonly_cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True, env=_tmux_env())
    finally:
        out.close(); err.close()
    time.sleep(0.5)
    thread_id = _latest_codex_thread_for_cwd(str(cwd), started_at)
    state = {"task_id": task.id, "board": board, "provider": "codex", "mode": "native-tmux", "state": "running", "tmux_name": str(tmux_name), "pid": proc.pid, "port": use_port, "url": url, "readonly_pid": readonly_proc.pid, "readonly_port": readonly_port, "readonly_url": readonly_url, "thread_id": thread_id, "cwd": str(cwd), "prompt_path": str(prompt_path), "prompt_submit": submit_info, "cmd": cmd, "stdout_log": str(stdout_path), "stderr_log": str(stderr_path), "started_at": started_at, "state_path": str(state_path)}
    _write_json_file(state_path, state)
    conn = kb.connect(board=board)
    try:
        _mark_running(conn, task.id)
        ensure_codex_session_link(conn, board, task.id, thread_id=thread_id or str(tmux_name), cwd=str(cwd))
        kb.add_comment(conn, task.id, author="kanban-agency", body=(
            "Codex native tmux session started.\n"
            f"URL: {codex_session_url(task.id)}\n"
            f"Direct ttyd: {url}\n"
            f"tmux: {tmux_name}\n"
            f"thread: {thread_id or ''}\n"
            f"cwd: {cwd}\n\n"
            "This tmux-backed TUI is the real execution surface; /s attaches to it."
        ))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "reused": False, "state": state, "url": codex_session_url(task.id), "ttyd_url": url, "tmux": str(tmux_name)}



def codex_native_init_role_session(board: str, task: kb.Task, meta: dict[str, str]) -> dict[str, Any]:
    """Start a native Codex TUI for an independent role without submitting work.

    Role shortcuts are conversation starters. They should initialize the persona
    and leave Codex at the input box so the user can type the actual task.
    """
    if not shutil.which("codex"):
        return {"ok": False, "error": "codex command not found"}
    if not shutil.which("ttyd"):
        return {"ok": False, "error": "ttyd command not found"}
    if not shutil.which("tmux"):
        return {"ok": False, "error": "tmux command not found"}
    cwd = Path(meta.get("workdir") or task.workspace_path or os.getcwd()).expanduser()
    cwd.mkdir(parents=True, exist_ok=True)
    state_path = _codex_web_state_path(task.id)
    state = _read_json_file(state_path)
    tmux_name = state.get("tmux_name") or f"kanban-codex-{task.id}"
    existing_pid = state.get("pid")
    if _tmux_has_session(str(tmux_name)) and existing_pid and _pid_alive(existing_pid) and state.get("url"):
        return {"ok": True, "reused": True, "state": state, "url": state.get("url")}

    CODEX_WEB_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = Path.home() / ".hermes" / "codex-kanban-runs" / task.id / "native-tui"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = run_dir / "role-init.md"
    prompt_path.write_text(_codex_prompt(task, meta), encoding="utf-8")
    started_at = int(time.time())
    if not _tmux_has_session(str(tmux_name)):
        subprocess.run(["tmux", "new-session", "-d", "-s", str(tmux_name), "-c", str(cwd), "bash", "-lc", "exec codex"], check=True)
        subprocess.run(["tmux", "set-option", "-t", str(tmux_name), "-g", "history-limit", "50000"], check=False)
        subprocess.run(["tmux", "set-option", "-t", str(tmux_name), "-g", "mouse", "off"], check=False)
        time.sleep(1.0)
        subprocess.run(["tmux", "load-buffer", "-t", str(tmux_name), str(prompt_path)], check=False)
        subprocess.run(["tmux", "paste-buffer", "-t", str(tmux_name)], check=False)
        # Deliberately do NOT press Enter: user starts the actual conversation.
    use_port = _free_port()
    url = f"http://127.0.0.1:{use_port}/"
    readonly_port = _free_port()
    readonly_url = f"http://127.0.0.1:{readonly_port}/"
    stdout_path = CODEX_WEB_DIR / f"{task.id}.stdout.log"
    stderr_path = CODEX_WEB_DIR / f"{task.id}.stderr.log"
    cmd = ["ttyd", "--interface", "127.0.0.1", "--port", str(use_port), "--writable", "-I", str(TTYD_WHEEL_INDEX), "-t", "scrollback=50000", "--client-option", f"titleFixed=Hermes {task.id}", "tmux", "attach-session", "-t", str(tmux_name)]
    readonly_cmd = ["ttyd", "--interface", "127.0.0.1", "--port", str(readonly_port), "-I", str(TTYD_WHEEL_INDEX), "-t", "scrollback=50000", "--client-option", f"titleFixed=Hermes {task.id} readonly", "tmux", "attach-session", "-t", str(tmux_name)]
    out = stdout_path.open("ab"); err = stderr_path.open("ab")
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True, env=_tmux_env())
        readonly_proc = subprocess.Popen(readonly_cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True, env=_tmux_env())
    finally:
        out.close(); err.close()
    time.sleep(0.5)
    thread_id = _latest_codex_thread_for_cwd(str(cwd), started_at)
    state = {"task_id": task.id, "board": board, "provider": "codex", "mode": "native-tmux-role-init", "state": "waiting_for_user", "tmux_name": str(tmux_name), "pid": proc.pid, "port": use_port, "url": url, "readonly_pid": readonly_proc.pid, "readonly_port": readonly_port, "readonly_url": readonly_url, "thread_id": thread_id, "cwd": str(cwd), "prompt_path": str(prompt_path), "cmd": cmd, "stdout_log": str(stdout_path), "stderr_log": str(stderr_path), "started_at": started_at, "state_path": str(state_path), "submitted": False}
    _write_json_file(state_path, state)
    conn = kb.connect(board=board)
    try:
        _mark_running(conn, task.id)
        ensure_codex_session_link(conn, board, task.id, thread_id=thread_id or str(tmux_name), cwd=str(cwd))
        kb.add_comment(conn, task.id, author="kanban-agency", body=(
            "Independent role Codex session initialized without submitting a task.\n"
            f"URL: {codex_session_url(task.id)}\n"
            f"Direct ttyd: {url}\n"
            f"tmux: {tmux_name}\n"
            f"cwd: {cwd}\n\n"
            "The role prompt is prefilled; the user must type/submit the first task."
        ))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "reused": False, "state": state, "url": codex_session_url(task.id), "ttyd_url": url, "tmux": str(tmux_name)}



def hermes_native_run_task(board: str, task: kb.Task, meta: dict[str, str]) -> dict[str, Any]:
    """Start/attach Hermes CLI in tmux + ttyd for a role task."""
    if not shutil.which("hermes"):
        return {"ok": False, "error": "hermes command not found"}
    if not shutil.which("ttyd"):
        return {"ok": False, "error": "ttyd command not found"}
    if not shutil.which("tmux"):
        return {"ok": False, "error": "tmux command not found"}
    cwd = Path(meta.get("workdir") or task.workspace_path or os.getcwd()).expanduser()
    cwd.mkdir(parents=True, exist_ok=True)
    state_path = _hermes_web_state_path(task.id)
    state = _read_json_file(state_path)
    tmux_name = state.get("tmux_name") or f"kanban-hermes-{task.id}"
    existing_pid = state.get("pid")
    if _tmux_has_session(str(tmux_name)) and existing_pid and _pid_alive(existing_pid) and state.get("url"):
        return {"ok": True, "reused": True, "state": state, "url": state.get("url")}

    HERMES_WEB_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = Path.home() / ".hermes" / "kanban-agency" / "hermes-runs" / task.id / "native-tui"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = run_dir / "prompt.md"
    prompt_path.write_text(_hermes_prompt(task, meta), encoding="utf-8")
    started_at = int(time.time())
    if not _tmux_has_session(str(tmux_name)):
        subprocess.run(["tmux", "new-session", "-d", "-s", str(tmux_name), "-c", str(cwd), "bash", "-lc", "exec hermes"], check=True)
        subprocess.run(["tmux", "set-option", "-t", str(tmux_name), "-g", "history-limit", "50000"], check=False)
        subprocess.run(["tmux", "set-option", "-t", str(tmux_name), "-g", "mouse", "off"], check=False)
        time.sleep(1.0)
        subprocess.run(["tmux", "load-buffer", "-t", str(tmux_name), str(prompt_path)], check=False)
        subprocess.run(["tmux", "paste-buffer", "-t", str(tmux_name)], check=False)
        subprocess.run(["tmux", "send-keys", "-t", str(tmux_name), "Enter"], check=False)
    use_port = _free_port()
    url = f"http://127.0.0.1:{use_port}/"
    readonly_port = _free_port()
    readonly_url = f"http://127.0.0.1:{readonly_port}/"
    stdout_path = HERMES_WEB_DIR / f"{task.id}.stdout.log"
    stderr_path = HERMES_WEB_DIR / f"{task.id}.stderr.log"
    cmd = ["ttyd", "--interface", "127.0.0.1", "--port", str(use_port), "--writable", "-I", str(TTYD_WHEEL_INDEX), "-t", "scrollback=50000", "--client-option", f"titleFixed=Hermes {task.id}", "tmux", "attach-session", "-t", str(tmux_name)]
    readonly_cmd = ["ttyd", "--interface", "127.0.0.1", "--port", str(readonly_port), "-I", str(TTYD_WHEEL_INDEX), "-t", "scrollback=50000", "--client-option", f"titleFixed=Hermes {task.id} readonly", "tmux", "attach-session", "-t", str(tmux_name)]
    out = stdout_path.open("ab"); err = stderr_path.open("ab")
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True, env=_tmux_env())
        readonly_proc = subprocess.Popen(readonly_cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True, env=_tmux_env())
    finally:
        out.close(); err.close()
    time.sleep(0.5)
    state = {"task_id": task.id, "board": board, "provider": "hermes", "mode": "native-tmux", "state": "running", "tmux_name": str(tmux_name), "pid": proc.pid, "port": use_port, "url": url, "readonly_pid": readonly_proc.pid, "readonly_port": readonly_port, "readonly_url": readonly_url, "cwd": str(cwd), "prompt_path": str(prompt_path), "cmd": cmd, "stdout_log": str(stdout_path), "stderr_log": str(stderr_path), "started_at": started_at, "state_path": str(state_path)}
    _write_json_file(state_path, state)
    conn = kb.connect(board=board)
    try:
        _mark_running(conn, task.id)
        kb.add_comment(conn, task.id, author="kanban-agency", body=(
            "Hermes native tmux session started.\n"
            f"URL: {codex_session_url(task.id)}\n"
            f"Direct ttyd: {url}\n"
            f"tmux: {tmux_name}\n"
            f"cwd: {cwd}\n"
        ))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "reused": False, "state": state, "url": codex_session_url(task.id), "ttyd_url": url, "tmux": str(tmux_name)}

def run(board: str, listen: str = "ws://127.0.0.1:8795", dry_run: bool = False, task_id: str | None = None) -> dict[str, Any]:
    if not board:
        return {"board": board, "started": [], "reused": [], "skipped": [], "errors": ["--board is required"]}
    if not kb.board_exists(board):
        return {"board": board, "started": [], "reused": [], "skipped": [], "errors": [f"board not found: {board}"]}
    started: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    runner = None
    conn = kb.connect(board=board)
    try:
        for row in _role_rows(conn, task_id=task_id):
            task = kb.Task.from_row(row)
            meta = _parse_role_body(task.body)
            provider = meta.get("provider")
            role = meta.get("role") or "unknown"
            if task.status not in {"ready", "running"}:
                skipped.append({"task_id": task.id, "role": role, "provider": provider, "reason": f"not runnable status {task.status}"})
                continue
            if provider == "claude":
                if dry_run:
                    started.append({"task_id": task.id, "role": role, "provider": provider, "dry_run": True})
                    continue
                try:
                    data = claude_interactive_run_task(board, task, meta)
                    if data.get("ok"):
                        try:
                            _mark_running(conn, task.id)
                        except Exception:
                            pass
                        target = reused if data.get("reused") else started
                        target.append({"task_id": task.id, "role": role, "provider": provider, "state": data.get("state")})
                    else:
                        errors.append(f"claude start failed for {task.id}: {data.get('error')}")
                except Exception as exc:
                    errors.append(f"claude start failed for {task.id}: {exc}")
                continue
            if provider == "hermes":
                if dry_run:
                    started.append({"task_id": task.id, "role": role, "provider": provider, "dry_run": True})
                    continue
                try:
                    data = hermes_native_run_task(board, task, meta)
                    if data.get("ok"):
                        try:
                            _mark_running(conn, task.id)
                        except Exception:
                            pass
                        target = reused if data.get("reused") else started
                        target.append({"task_id": task.id, "role": role, "provider": provider, "state": data.get("state"), "url": data.get("url")})
                    else:
                        errors.append(f"hermes native start failed for {task.id}: {data.get('error')}")
                except Exception as exc:
                    errors.append(f"hermes native start failed for {task.id}: {exc}")
                continue
            if provider != "codex":
                skipped.append({"task_id": task.id, "role": role, "provider": provider, "reason": "unsupported provider in MVP"})
                continue
            if dry_run:
                started.append({"task_id": task.id, "role": role, "provider": provider, "dry_run": True})
                continue
            try:
                data = codex_native_run_task(board, task, meta)
                if data.get("ok"):
                    try:
                        _mark_running(conn, task.id)
                    except Exception:
                        pass
                    target = reused if data.get("reused") else started
                    target.append({"task_id": task.id, "role": role, "provider": provider, "state": data.get("state"), "url": data.get("url")})
                else:
                    errors.append(f"codex native start failed for {task.id}: {data.get('error')}")
            except Exception as exc:
                errors.append(f"codex native start failed for {task.id}: {exc}")
    finally:
        conn.close()
    return {"board": board, "started": started, "reused": reused, "skipped": skipped, "errors": errors, "dry_run": dry_run}


def _set_status(conn, task_id: str, status: str, result: str | None = None) -> None:
    old_row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    old_status = old_row["status"] if old_row else None
    if result is None:
        conn.execute("UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, worker_pid = NULL WHERE id = ?", (status, task_id))
    else:
        conn.execute("UPDATE tasks SET status = ?, result = ?, claim_lock = NULL, claim_expires = NULL, worker_pid = NULL WHERE id = ?", (status, result, task_id))
    if old_status != status:
        try:
            if status == "blocked":
                kb._append_event(conn, task_id, "blocked", {"source": "kanban-agency"})
            elif old_status == "blocked" and status != "blocked":
                kb._append_event(conn, task_id, "unblocked", {"source": "kanban-agency", "to": status})
        except Exception:
            pass


def _mark_running(conn, task_id: str) -> None:
    with kb.write_txn(conn):
        _set_status(conn, task_id, "running")


def _sync_one_codex_task(kb_mod, conn, runner, board: str, task: kb.Task, meta: dict[str, str]) -> dict[str, Any]:
    provider = meta.get("provider")
    role = meta.get("role") or "unknown"
    if provider != "codex":
        return {"task_id": task.id, "role": role, "provider": provider, "action": "skipped", "reason": "unsupported provider in MVP"}
    status_raw = runner.codex_kanban_appserver_task_status({"task_id": task.id})
    status = json.loads(status_raw)
    state = (status.get("state") or {}).get("state")
    final_tail = (status.get("final_tail") or "").strip()
    if state == "running":
        if task.status != "running":
            _mark_running(conn, task.id)
            return {"task_id": task.id, "role": role, "provider": provider, "action": "marked_running", "state": state}
        return {"task_id": task.id, "role": role, "provider": provider, "action": "already_running", "state": state}
    if state == "awaiting_review":
        summary = final_tail or "Codex role turn completed; waiting for user Complete."
        # User rule: no bell / no explicit user gate means the role stays in
        # progress. The user clicks Kanban Complete when they accept the result;
        # Complete is the only signal that the phase is done.
        with kb_mod.write_txn(conn):
            _set_status(conn, task.id, "running", result=summary)
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task.id, "kanban-agency", f"Codex role turn completed without a user-blocking signal; keeping task running until user clicks Complete.\n\n{summary}", int(__import__('time').time())),
            )
        return {"task_id": task.id, "role": role, "provider": provider, "action": "synced_running_waiting_complete", "state": state}
    if state == "blocked":
        reason = final_tail or "Codex is waiting for user input/approval."
        try:
            kb_mod.block_task(conn, task.id, reason=reason)
        except Exception:
            with kb_mod.write_txn(conn):
                _set_status(conn, task.id, "blocked", result=reason)
        return {"task_id": task.id, "role": role, "provider": provider, "action": "synced_blocked", "state": state}
    if state in {"exited", "failed", "timeout"}:
        reason = final_tail or f"Codex bridge state: {state}"
        # A timeout is not a user bell. If a native Codex session is still alive,
        # keep the card running; otherwise block with the failure reason.
        web_state = _read_json_file(_codex_web_state_path(task.id))
        if state == "timeout" and web_state.get("pid") and _pid_alive(web_state.get("pid")):
            with kb_mod.write_txn(conn):
                _set_status(conn, task.id, "running", result=reason)
            return {"task_id": task.id, "role": role, "provider": provider, "action": "timeout_but_web_session_alive_keep_running", "state": state}
        try:
            kb_mod.block_task(conn, task.id, reason=reason)
        except Exception:
            with kb_mod.write_txn(conn):
                _set_status(conn, task.id, "blocked", result=reason)
        return {"task_id": task.id, "role": role, "provider": provider, "action": "synced_blocked", "state": state}
    return {"task_id": task.id, "role": role, "provider": provider, "action": "no_state_change", "state": state}


def sync(board: str, task_id: str | None = None) -> dict[str, Any]:
    if not board:
        return {"board": board, "synced": [], "errors": ["--board is required"]}
    if not kb.board_exists(board):
        return {"board": board, "synced": [], "errors": [f"board not found: {board}"]}
    try:
        runner = _load_codex_runner()
    except Exception as exc:
        return {"board": board, "synced": [], "errors": [str(exc)]}
    synced: list[dict[str, Any]] = []
    errors: list[str] = []
    conn = kb.connect(board=board)
    try:
        for row in _role_rows(conn, task_id=task_id):
            task = kb.Task.from_row(row)
            meta = _parse_role_body(task.body)
            try:
                synced.append(_sync_one_codex_task(kb, conn, runner, board, task, meta))
            except Exception as exc:
                errors.append(f"sync failed for {task.id}: {exc}")
    finally:
        conn.close()
    return {"board": board, "synced": synced, "errors": errors}


CODEX_WEB_DIR = Path.home() / ".hermes" / "kanban-agency" / "codex-web"
HERMES_WEB_DIR = Path.home() / ".hermes" / "kanban-agency" / "hermes-web"
SESSION_BINDING_DIR = Path.home() / ".hermes" / "kanban-agency" / "session-bindings"


def _session_binding_path(thread_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(thread_id))
    return SESSION_BINDING_DIR / f"{safe}.json"


def _read_session_binding(thread_id: str | None) -> dict[str, Any]:
    if not thread_id:
        return {}
    return _read_json_file(_session_binding_path(thread_id))


def _write_session_binding(thread_id: str | None, *, task_id: str, board: str | None, role: str | None = None, root_id: str | None = None) -> None:
    if not thread_id:
        return
    data = {
        "thread_id": thread_id,
        "active_task_id": task_id,
        "board": board,
        "role": role,
        "root_id": root_id,
        "updated_at": int(time.time()),
    }
    _write_json_file(_session_binding_path(thread_id), data)


def _codex_web_state_path(task_id: str) -> Path:
    return CODEX_WEB_DIR / f"{task_id}.json"


def _hermes_web_state_path(task_id: str) -> Path:
    return HERMES_WEB_DIR / f"{task_id}.json"


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _state_paths_for_task(task_id: str) -> list[Path]:
    paths = [_codex_web_state_path(task_id), _hermes_web_state_path(task_id)]
    try:
        paths.append(_claude_web_state_path(task_id))
        paths.append(_claude_state_path(task_id))
    except Exception:
        pass
    # Preserve order while deduplicating; Claude web/state may coincide in future.
    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _terminate_pid(pid: Any, *, timeout: float = 2.0) -> bool:
    if not pid:
        return False
    try:
        pid_i = int(pid)
    except Exception:
        return False
    if not _pid_alive(pid_i):
        return False
    try:
        os.killpg(pid_i, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid_i, signal.SIGTERM)
        except Exception:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline and _pid_alive(pid_i):
        time.sleep(0.05)
    if _pid_alive(pid_i):
        try:
            os.killpg(pid_i, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid_i, signal.SIGKILL)
            except Exception:
                pass
    return not _pid_alive(pid_i)


def _stop_provider_session_state(task_id: str, *, reason: str, now: int | None = None, kill_tmux: bool = True, dry_run: bool = False) -> dict[str, Any]:
    now_i = int(now or time.time())
    state_paths = [p for p in _state_paths_for_task(task_id) if p.exists()]
    states: list[dict[str, Any]] = []
    for path in state_paths:
        state = _read_json_file(path)
        if not state:
            continue
        states.append({"path": str(path), "state": state})
    tmux_names = sorted({str((item["state"].get("tmux_name") or item["state"].get("tmux") or "")).strip() for item in states if (item["state"].get("tmux_name") or item["state"].get("tmux"))})
    pids = []
    for item in states:
        state = item["state"]
        for key in ("pid", "readonly_pid"):
            if state.get(key):
                pids.append((key, state.get(key)))
    if dry_run:
        return {"task_id": task_id, "dry_run": True, "state_paths": [str(p) for p in state_paths], "tmux_names": tmux_names, "pids": pids}
    stopped_pids = []
    for key, pid in pids:
        stopped_pids.append({"key": key, "pid": pid, "stopped": _terminate_pid(pid)})
    killed_tmux = []
    if kill_tmux:
        for tmux_name in tmux_names:
            existed = _tmux_has_session(tmux_name)
            ok = False
            if existed:
                cp = subprocess.run(["tmux", "kill-session", "-t", tmux_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_tmux_env())
                ok = cp.returncode == 0 or not _tmux_has_session(tmux_name)
            killed_tmux.append({"tmux_name": tmux_name, "existed": existed, "stopped": ok})
    for item in states:
        path = Path(item["path"])
        state = dict(item["state"])
        state.update({"state": "stopped", "stopped_at": now_i, "stop_reason": reason, "tmux_stopped": killed_tmux})
        _write_json_file(path, state)
    return {"task_id": task_id, "state_paths": [str(p) for p in state_paths], "stopped_pids": stopped_pids, "tmux": killed_tmux}


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _load_bridge_state(task_id: str) -> dict[str, Any]:
    p = Path.home() / ".hermes" / "codex-kanban-runs" / task_id / "appserver-bridge" / "bridge.json"
    return _read_json_file(p)


def codex_web(board: str, task_id: str, port: int | None = None, reuse: bool = True) -> dict[str, Any]:
    if not board:
        return {"ok": False, "error": "--board is required"}
    if not task_id:
        return {"ok": False, "error": "--task-id is required"}
    ttyd = shutil.which("ttyd")
    if not ttyd:
        return {"ok": False, "error": "ttyd not found; install with: brew install ttyd"}
    codex = shutil.which("codex")
    if not codex:
        return {"ok": False, "error": "codex command not found"}

    state_path = _codex_web_state_path(task_id)
    old = _read_json_file(state_path)
    if reuse and old.get("pid") and _pid_alive(old.get("pid")) and (not old.get("tmux_name") or _tmux_has_session(str(old.get("tmux_name")))):
        return {"ok": True, "reused": True, "state": old, "url": old.get("url")}

    conn = kb.connect(board=board)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": f"task not found: {task_id}"}
        task = kb.Task.from_row(row)
        bridge = _load_bridge_state(task_id)
        thread_id = str(old.get("thread_id") or bridge.get("thread_id") or "").strip()
        if not thread_id and old.get("mode") == "native-tmux":
            thread_id = _latest_codex_thread_for_cwd(str(old.get("cwd") or ""), int(old.get("started_at") or 0)) or ""
        meta = _parse_role_body(task.body or "")
        cwd = Path(str(old.get("cwd") or bridge.get("cwd") or meta.get("workdir") or task.workspace_path or os.getcwd())).expanduser()
        if not cwd.exists():
            return {"ok": False, "error": f"cwd does not exist: {cwd}"}
        if not thread_id and old.get("mode") == "native-tmux" and shutil.which("tmux"):
            old_tmux = str(old.get("tmux_name") or f"kanban-codex-{task_id}")
            if not _tmux_has_session(old_tmux):
                return {"ok": False, "error": f"native Codex session for {task_id} was lost before a thread_id was captured; reflow this role instead of resuming", "task_id": task_id, "cwd": str(cwd), "reason": "lost_native_session_without_thread", "reflow_required": True}
        if not thread_id:
            return {"ok": False, "error": f"no codex thread_id for task {task_id}; run provider first", "bridge_state": bridge, "web_state": old}
        if os.environ.get("KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN") in {"1", "true", "yes"}:
            return {"ok": False, "error": "provider spawn disabled by KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN", "task_id": task_id, "thread_id": thread_id, "cwd": str(cwd)}
        use_port = int(port or _free_port())
        url = f"http://127.0.0.1:{use_port}/"
        CODEX_WEB_DIR.mkdir(parents=True, exist_ok=True)
        stdout_path = CODEX_WEB_DIR / f"{task_id}.stdout.log"
        stderr_path = CODEX_WEB_DIR / f"{task_id}.stderr.log"
        tmux_name = str(old.get("tmux_name") or old.get("tmux") or f"kanban-codex-{task_id}")
        if shutil.which("tmux"):
            if not _tmux_has_session(tmux_name):
                subprocess.run([
                    "tmux", "new-session", "-d", "-s", tmux_name,
                    "-c", str(cwd),
                    "bash", "-lc", f"exec codex resume {shlex.quote(thread_id)}",
                ], check=True, env=_tmux_env())
            cmd = [
                ttyd,
                "--interface", "127.0.0.1",
                "--port", str(use_port),
                "--writable",
                "-I", str(TTYD_WHEEL_INDEX),
                "-t", "scrollback=50000",
                "--client-option", f"titleFixed=Codex {task_id}",
                "tmux", "attach-session", "-t", tmux_name,
            ]
        else:
            cmd = [
                ttyd,
                "--interface", "127.0.0.1",
                "--port", str(use_port),
                "--writable",
                "--client-option", f"titleFixed=Codex {task_id}",
                "--cwd", str(cwd),
                "bash", "-lc", f"exec codex resume {shlex.quote(thread_id)}",
            ]
        readonly_proc = None
        readonly_url = None
        readonly_port = None
        if shutil.which("tmux"):
            readonly_port = _free_port()
            readonly_url = f"http://127.0.0.1:{readonly_port}/"
            readonly_cmd = [ttyd, "--interface", "127.0.0.1", "--port", str(readonly_port), "-I", str(TTYD_WHEEL_INDEX), "-t", "scrollback=50000", "--client-option", f"titleFixed=Codex {task_id} readonly", "tmux", "attach-session", "-t", tmux_name]
        out = stdout_path.open("ab")
        err = stderr_path.open("ab")
        try:
            proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True, env=_tmux_env())
            if readonly_url:
                readonly_proc = subprocess.Popen(readonly_cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True, env=_tmux_env())
        finally:
            out.close(); err.close()
        state = {
            "task_id": task_id,
            "board": board,
            "pid": proc.pid,
            "port": use_port,
            "url": url,
            "readonly_pid": readonly_proc.pid if readonly_proc else None,
            "readonly_port": readonly_port,
            "readonly_url": readonly_url,
            "thread_id": thread_id,
            "cwd": str(cwd),
            "cmd": cmd,
            "tmux_name": tmux_name if shutil.which("tmux") else None,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "started_at": int(time.time()),
            "state_path": str(state_path),
        }
        _write_json_file(state_path, state)
        kb.add_comment(conn, task_id, author="kanban-agency", body=(
            "Codex Web TUI is ready.\n"
            f"URL: {url}\n"
            f"task: {task_id}\n"
            f"thread: {thread_id}\n"
            f"cwd: {cwd}\n\n"
            "Open this local URL to use the native Codex TUI for approval/chat. "
            "Hermes/Kanban should monitor and sync state; do not approve via comment regex."
        ))
        return {"ok": True, "reused": False, "url": url, "state": state}
    finally:
        conn.close()


def codex_web_stop(board: str, task_id: str) -> dict[str, Any]:
    if not task_id:
        return {"ok": False, "error": "--task-id is required"}
    state_path = _codex_web_state_path(task_id)
    state = _read_json_file(state_path)
    if not state:
        return {"ok": False, "error": f"no codex-web state for {task_id}"}
    pid = state.get("pid")
    stopped = False
    if pid and _pid_alive(pid):
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except Exception:
                pass
        deadline = time.time() + 3
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        if _pid_alive(pid):
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except Exception:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except Exception:
                    pass
        stopped = not _pid_alive(pid)
    state.update({"stopped_at": int(time.time()), "state": "stopped" if stopped else "not-running"})
    _write_json_file(state_path, state)
    if board and kb.board_exists(board):
        conn = kb.connect(board=board)
        try:
            kb.add_comment(conn, task_id, author="kanban-agency", body=f"Codex Web TUI stopped.\nURL was: {state.get('url')}\npid: {pid}")
        finally:
            conn.close()
    return {"ok": True, "state": state}

def continue_comments(board: str, listen: str = "ws://127.0.0.1:8795", dry_run: bool = False, task_id: str | None = None) -> dict[str, Any]:
    if not board:
        return {"board": board, "continued": [], "skipped": [], "errors": ["--board is required"], "dry_run": dry_run}
    if not kb.board_exists(board):
        return {"board": board, "continued": [], "skipped": [], "errors": [f"board not found: {board}"], "dry_run": dry_run}
    continued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    runner = None
    conn = kb.connect(board=board)
    try:
        for row in _role_rows(conn, task_id=task_id):
            task = kb.Task.from_row(row)
            meta = _parse_role_body(task.body)
            provider = meta.get("provider")
            role = meta.get("role") or "unknown"
            if provider == "claude":
                state = _read_claude_state(task.id)
                comments = kb.list_comments(conn, task.id)
                last_seen = int(state.get("last_human_comment_id") or 0)
                candidates = [c for c in comments if c.id > last_seen and c.author not in {"kanban-agency"} and (c.body or "").strip()]
                if not candidates:
                    skipped.append({"task_id": task.id, "role": role, "provider": provider, "reason": "no new human comment", "last_human_comment_id": last_seen})
                    continue
                comment = candidates[-1]
                if dry_run:
                    continued.append({"task_id": task.id, "role": role, "provider": provider, "comment_id": comment.id, "dry_run": True})
                    continue
                tmux_name = state.get("tmux")
                if not tmux_name or not _tmux_has_session(str(tmux_name)):
                    errors.append(f"claude continue failed for {task.id}: no active tmux session; open/run Claude session first")
                    continue
                d = CLAUDE_RUN_DIR / task.id; d.mkdir(parents=True, exist_ok=True)
                msg_path = d / f"comment-{comment.id}.txt"; msg_path.write_text(comment.body or "", encoding="utf-8")
                subprocess.run(["tmux", "load-buffer", "-t", str(tmux_name), str(msg_path)], check=False)
                subprocess.run(["tmux", "paste-buffer", "-t", str(tmux_name)], check=False)
                subprocess.run(["tmux", "send-keys", "-t", str(tmux_name), "Enter"], check=False)
                state.update({"state": "blocked", "reason": "interactive_session", "last_human_comment_id": comment.id, "continued_at": int(time.time())})
                _write_json_file(_claude_state_path(task.id), state)
                with kb.write_txn(conn):
                    _set_status(conn, task.id, "blocked", result=f"Forwarded comment {comment.id} to Claude TUI: {claude_session_url(task.id)}")
                    conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)", (task.id, "kanban-agency", f"Forwarded comment {comment.id} to Claude interactive session.\nURL: {claude_session_url(task.id)}"))
                continued.append({"task_id": task.id, "role": role, "provider": provider, "comment_id": comment.id, "state": state, "url": claude_session_url(task.id)})
                continue
            if provider != "codex":
                skipped.append({"task_id": task.id, "role": role, "provider": provider, "reason": "unsupported provider in MVP"})
                continue
            if runner is None:
                try:
                    runner = _load_codex_runner()
                except Exception as exc:
                    errors.append(str(exc))
                    break
            state = runner._read_bridge_state(task.id)
            session_ref = _resolve_codex_session_ref(conn, task, meta)
            if not state.get("thread_id") and not session_ref.get("thread_id"):
                skipped.append({"task_id": task.id, "role": role, "provider": provider, "reason": "no codex thread yet; run first"})
                continue
            comments = kb.list_comments(conn, task.id)
            last_seen = int(state.get("last_human_comment_id") or 0)
            candidates = [c for c in comments if c.id > last_seen and c.author not in {"codex-kanban-runner", "kanban-agency"} and (c.body or "").strip()]
            if not candidates:
                skipped.append({"task_id": task.id, "role": role, "provider": provider, "reason": "no new human comment", "last_human_comment_id": last_seen})
                continue
            comment = candidates[-1]
            if dry_run:
                continued.append({"task_id": task.id, "role": role, "provider": provider, "comment_id": comment.id, "dry_run": True})
                continue
            payload = {
                "task_id": task.id,
                "board": board,
                "listen": listen,
                "appserver_name": f"agency-{task.id}",
                "cwd": meta.get("workdir") or task.workspace_path or state.get("cwd") or None,
                "timeout_seconds": 24 * 60 * 60,
                "after_comment_id": state.get("last_human_comment_id"),
                "resume_thread_id": session_ref.get("thread_id"),
            }
            try:
                raw = runner.codex_kanban_appserver_continue_task(payload)
                data = json.loads(raw)
                if data.get("ok"):
                    thread_id = data.get("thread_id") or (data.get("state") or {}).get("thread_id") or session_ref.get("thread_id")
                    _write_session_binding(thread_id, task_id=task.id, board=board, role=role, root_id=meta.get("root_id"))
                    try:
                        _mark_running(conn, task.id)
                    except Exception:
                        pass
                    continued.append({"task_id": task.id, "role": role, "provider": provider, "comment_id": data.get("human_comment_id"), "thread_id": thread_id, "state": data.get("state")})
                else:
                    errors.append(f"codex continue failed for {task.id}: {data.get('error')}")
            except Exception as exc:
                errors.append(f"codex continue failed for {task.id}: {exc}")
    finally:
        conn.close()
    return {"board": board, "continued": continued, "skipped": skipped, "errors": errors, "dry_run": dry_run}



CLAUDE_WEB_DIR = Path.home() / ".hermes" / "kanban-agency" / "claude-web"

def _find_board_for_task(task_id: str) -> str | None:
    try:
        boards = kb.list_boards()
    except Exception:
        boards = []
    for b in boards:
        slug = b.get("slug") if isinstance(b, dict) else getattr(b, "slug", None) or str(b)
        if not slug:
            continue
        try:
            conn = kb.connect(board=slug)
            try:
                row = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
                if row:
                    return slug
            finally:
                conn.close()
        except Exception:
            continue
    return None

def _claude_web_state_path(task_id: str) -> Path:
    return CLAUDE_WEB_DIR / f"{task_id}.json"

def claude_session_url(task_id: str, port: int = 8766) -> str:
    return f"http://127.0.0.1:{int(port)}/s/{task_id}"





def _process_command_contains(pid: Any, needle: str) -> bool:
    try:
        cp = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="], text=True, capture_output=True, timeout=2)
        return cp.returncode == 0 and needle in (cp.stdout or "")
    except Exception:
        return False

_CODEX_RESUME_PIDS_CACHE: tuple[float, dict[str, list[int]]] = (0.0, {})
_CODEX_RESUME_PIDS_CACHE_TTL = 0.75


def _codex_resume_pids_by_thread() -> dict[str, list[int]]:
    global _CODEX_RESUME_PIDS_CACHE
    now = time.time()
    ts, cached = _CODEX_RESUME_PIDS_CACHE
    if now - ts < _CODEX_RESUME_PIDS_CACHE_TTL:
        return cached
    out: dict[str, list[int]] = {}
    try:
        cp = subprocess.run(["pgrep", "-af", "codex resume"], text=True, capture_output=True, timeout=2)
        if cp.returncode == 0:
            for line in (cp.stdout or "").splitlines():
                parts = line.split(maxsplit=1)
                if len(parts) != 2:
                    continue
                try:
                    pid = int(parts[0])
                except Exception:
                    continue
                cmd = parts[1]
                m = re.search(r"\bcodex\s+resume\s+([^\s]+)", cmd)
                if m:
                    out.setdefault(m.group(1), []).append(pid)
    except Exception:
        out = {}
    _CODEX_RESUME_PIDS_CACHE = (now, out)
    return out


def _codex_native_session_live(task_id: str, thread_id: str | None = None, require_provider_process: bool = False) -> dict[str, Any]:
    state = _read_json_file(_codex_web_state_path(task_id))
    pid = state.get("pid")
    ttyd_alive = bool(pid and _pid_alive(pid))
    tmux_name = state.get("tmux_name") or state.get("tmux")
    tmux_alive = bool(tmux_name and _tmux_has_session(str(tmux_name)))
    thread_id = state.get("thread_id") or thread_id
    codex_alive = False
    codex_pids: list[int] = []
    if thread_id:
        pids_by_thread = _codex_resume_pids_by_thread()
        codex_pids = list(pids_by_thread.get(thread_id) or [])
        codex_alive = bool(codex_pids)
    live_bool = bool(codex_alive or tmux_alive or (ttyd_alive and not require_provider_process))
    return {"live": live_bool, "ttyd_alive": ttyd_alive, "ttyd_pid": pid, "tmux_alive": tmux_alive, "tmux_name": tmux_name, "codex_alive": codex_alive, "codex_pids": codex_pids, "thread_id": thread_id, "url": state.get("url")}




def _codex_native_session_live_for_status(task_id: str, thread_id: str | None = None) -> dict[str, Any]:
    try:
        return _codex_native_session_live(task_id, thread_id, require_provider_process=True)
    except TypeError:
        live = _codex_native_session_live(task_id, thread_id)
        if live.get("ttyd_alive") and not (live.get("tmux_alive") or live.get("codex_alive")):
            live = dict(live)
            live["live"] = False
        return live

# Cockpit gateway currently runs as a single-threaded HTTPServer. These short
# TTL process/session caches intentionally do not use locks; add locking before
# switching the gateway back to ThreadingHTTPServer.
_CODEX_SESSION_INDEX_CACHE: tuple[float, dict[str, Path]] = (0.0, {})
_CODEX_SESSION_INDEX_CACHE_TTL = 2.0
_CODEX_SESSION_FILE_CACHE: dict[str, tuple[float, Path | None]] = {}


def _codex_session_index() -> dict[str, Path]:
    """Return a short-lived thread_id -> latest session file index.

    Cockpit calls /sessions on a timer and may need status for dozens of roles.
    A per-thread Path.rglob over ~/.codex/sessions is O(tasks * session files)
    and was the dominant gateway CPU cost. Scan once per TTL instead.
    """
    global _CODEX_SESSION_INDEX_CACHE
    now = time.time()
    ts, cached = _CODEX_SESSION_INDEX_CACHE
    if now - ts < _CODEX_SESSION_INDEX_CACHE_TTL:
        return cached
    root = Path.home() / ".codex" / "sessions"
    out: dict[str, Path] = {}
    mtimes: dict[str, float] = {}
    if root.exists():
        try:
            for fp in root.rglob("*.jsonl"):
                thread_id = _codex_thread_id_from_path(fp)
                if not thread_id:
                    continue
                try:
                    mtime = fp.stat().st_mtime
                except Exception:
                    mtime = 0.0
                if thread_id not in out or mtime > mtimes.get(thread_id, -1):
                    out[thread_id] = fp
                    mtimes[thread_id] = mtime
        except Exception:
            out = {}
    _CODEX_SESSION_INDEX_CACHE = (now, out)
    _CODEX_SESSION_FILE_CACHE.clear()
    return out


def _find_codex_session_file(thread_id: str | None) -> Path | None:
    if not thread_id:
        return None
    thread = str(thread_id)
    now = time.time()
    cached = _CODEX_SESSION_FILE_CACHE.get(thread)
    if cached and now - cached[0] < _CODEX_SESSION_INDEX_CACHE_TTL:
        return cached[1]
    path = _codex_session_index().get(thread)
    if path is None:
        # Legacy state may store a prefix or non-canonical thread id. Fall back
        # to substring matching against the indexed filenames without walking the
        # tree again.
        matches = [p for tid, p in _codex_session_index().items() if thread in tid or thread in p.name]
        if matches:
            try:
                path = max(matches, key=lambda p: p.stat().st_mtime)
            except Exception:
                path = matches[0]
    _CODEX_SESSION_FILE_CACHE[thread] = (now, path)
    return path

def _parse_codex_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        return None


def _provider_pending_acknowledged(task_status: str | None, task_completed_at: Any, web: dict[str, Any], pending: dict[str, Any]) -> bool:
    if not pending.get("pending"):
        return False
    if task_status != "done":
        return False
    # Kanban `done` is the human acceptance signal. Once a concrete task is
    # done, provider-side historical approvals/completion events should not keep
    # ringing in Cockpit; new work must be represented by a new task/session.
    return True


def _codex_thread_id_from_path(path: Path) -> str | None:
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path.name)
    return m.group(1) if m else None


def _recover_codex_thread_for_task(task_id: str, web: dict[str, Any]) -> str | None:
    """Recover missing Codex thread_id by matching live tmux text to session JSONL.

    This repairs the state association; it does not infer bell state from screen text.
    """
    existing = web.get("thread_id")
    if existing:
        return str(existing)
    tmux_name = web.get("tmux_name") or web.get("tmux") or f"kanban-codex-{task_id}"
    if not tmux_name or not _tmux_has_session(str(tmux_name)):
        return None
    try:
        tail = subprocess.check_output(["tmux", "capture-pane", "-t", str(tmux_name), "-p", "-S", "-160"], text=True, stderr=subprocess.STDOUT, env=_tmux_env())
    except Exception:
        return None
    lines = []
    for line in tail.splitlines():
        t = line.strip()
        if len(t) < 10:
            continue
        if t.startswith(("›", "•", "─", "Use /skills")):
            continue
        lines.append(t[:160])
    if not lines:
        return None
    cwd = str(web.get("cwd") or "")
    since = int(web.get("started_at") or 0)
    root = Path.home() / ".codex" / "sessions"
    if not root.exists():
        return None
    best: tuple[int, float, Path] | None = None
    now = time.time()
    for fp in root.rglob("*.jsonl"):
        try:
            st = fp.stat()
            if since and st.st_mtime < since - 600:
                continue
            if st.st_mtime < now - 14 * 24 * 3600:
                continue
            txt = fp.read_text(encoding="utf-8", errors="replace")
            if cwd and cwd not in txt:
                continue
            score = 0
            for line in lines[-40:]:
                if line and line in txt:
                    score += min(len(line), 120)
            if score <= 0:
                continue
            if best is None or score > best[0] or (score == best[0] and st.st_mtime > best[1]):
                best = (score, st.st_mtime, fp)
        except Exception:
            continue
    if not best:
        return None
    thread_id = _codex_thread_id_from_path(best[2])
    if thread_id:
        web = dict(web)
        web["thread_id"] = thread_id
        web["thread_recovered_at"] = int(time.time())
        web["thread_recovery_method"] = "tmux_tail_jsonl_overlap"
        web["thread_recovery_session_file"] = str(best[2])
        try:
            _write_json_file(_codex_web_state_path(task_id), web)
        except Exception:
            pass
    return thread_id


_CODEX_PENDING_APPROVAL_CACHE: dict[str, tuple[str, int, int, dict[str, Any]]] = {}


def _codex_live_pending_approval(thread_id: str | None) -> dict[str, Any]:
    """Detect approval prompts emitted by native `codex resume` session logs.

    The appserver bridge event log stops once we hand off to the native TUI.
    New approval bells in the browser appear in ~/.codex/sessions as a
    response_item function_call with sandbox_permissions=require_escalated and
    no later function_call_output for the same call_id.
    """
    path = _find_codex_session_file(thread_id)
    if not path:
        return {"pending": False, "reason": "no_session_file"}
    try:
        st = path.stat()
        cache_key = str(thread_id or "")
        cached = _CODEX_PENDING_APPROVAL_CACHE.get(cache_key)
        if cached and cached[0] == str(path) and cached[1] == st.st_mtime_ns and cached[2] == st.st_size:
            return dict(cached[3])
    except Exception:
        st = None
        cache_key = str(thread_id or "")
    pending: dict[str, dict[str, Any]] = {}
    last_task_complete: dict[str, Any] | None = None
    saw_task_complete = False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return {"pending": False, "reason": f"read_failed: {exc}", "path": str(path)}
    for line in lines[-500:]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        payload = obj.get("payload") or {}
        typ = payload.get("type")
        call_id = payload.get("call_id")
        if typ == "function_call" and call_id:
            args_raw = payload.get("arguments") or "{}"
            try:
                args = json.loads(args_raw)
            except Exception:
                args = {"raw": args_raw}
            sandbox = str(args.get("sandbox_permissions") or "")
            if sandbox == "require_escalated":
                pending[call_id] = {"timestamp": obj.get("timestamp"), "call_id": call_id, "name": payload.get("name"), "cmd": args.get("cmd"), "workdir": args.get("workdir"), "justification": args.get("justification"), "session_file": str(path)}
        elif typ == "function_call_output" and call_id:
            pending.pop(call_id, None)
        elif typ == "custom_tool_call" and call_id:
            tool_input = payload.get("input") or ""
            pending[call_id] = {
                "timestamp": obj.get("timestamp"),
                "call_id": call_id,
                "kind": "tool_call_approval_required",
                "name": payload.get("name"),
                "cmd": None,
                "workdir": None,
                "justification": str(tool_input)[:500],
                "session_file": str(path),
            }
        elif typ == "custom_tool_call_output" and call_id:
            pending.pop(call_id, None)
        elif obj.get("type") == "event_msg" and payload.get("type") == "task_complete":
            saw_task_complete = True
            last_task_complete = {
                "timestamp": obj.get("timestamp"),
                "kind": "role_completed_waiting_complete",
                "last_agent_message": payload.get("last_agent_message"),
                "completed_at": payload.get("completed_at"),
                "session_file": str(path),
            }
        elif obj.get("type") == "event_msg" and payload.get("type") in {"task_started", "user_message"}:
            if last_task_complete:
                last_task_complete = None
        elif obj.get("type") == "event_msg" and payload.get("type") in {"exec_command_end", "patch_apply_end"}:
            cid = payload.get("call_id")
            if cid:
                pending.pop(cid, None)
    if pending:
        last = list(pending.values())[-1]
        result = {"pending": True, "kind": "approval_required", **last}
    elif last_task_complete:
        result = {"pending": True, **last_task_complete}
    else:
        reason = "appended_turn_after_task_complete" if saw_task_complete else "no_pending_approval"
        result = {"pending": False, "reason": reason, "session_file": str(path)}
    if st is not None:
        _CODEX_PENDING_APPROVAL_CACHE[cache_key] = (str(path), st.st_mtime_ns, st.st_size, dict(result))
    return result

def _parents_satisfied(conn, task_id: str) -> bool:
    """Whether real upstream role dependencies are satisfied.

    kanban-agency uses task_links both for grouping and sequencing. A root task
    linked to the first role (analyst) is an aggregate/grouping anchor, not a
    dependency that must be done before analyst can run. Only role->role parents
    block downstream roles.
    """
    rows = conn.execute(
        "SELECT t.id,t.title,t.body,t.status FROM tasks t JOIN task_links l ON l.parent_id=t.id WHERE l.child_id=?",
        (task_id,),
    ).fetchall()
    blocking = []
    for r in rows:
        title = r["title"] or ""
        body = r["body"] or ""
        is_agency_role = title.startswith("[agency] ") or "@kanban-agency-role" in body
        if not is_agency_role:
            # Root/grouping anchors do not block the first role.
            continue
        blocking.append(r)
    return all(r["status"] in {"done", "archived"} for r in blocking)


def _reset_waiting_on_upstream(conn, task: kb.Task) -> bool:
    """Keep future workflow roles as todo while upstream dependency is unfinished."""
    if task.status in {"done", "archived"}:
        return False
    if _parents_satisfied(conn, task.id):
        return False
    bridge = _load_bridge_state(task.id)
    live = _codex_native_session_live(task.id, bridge.get("thread_id"))
    if live.get("live"):
        return False
    if task.status != "todo":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=?, result=? WHERE id=?", ("todo", "Waiting for upstream role to complete.", task.id))
    return True


def monitor(board: str, task_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Synchronise Kanban state from native provider session liveness.

    Important case: appserver bridge may be stale-blocked on an old approval,
    while the user has continued work in the browser via codex resume. If the
    native Codex session is live, keep the task in running so Kanban reflects
    actual work in progress. User Complete remains the only done signal.
    """
    if not board:
        return {"board": board, "monitored": [], "errors": ["--board is required"], "dry_run": dry_run}
    if not kb.board_exists(board):
        return {"board": board, "monitored": [], "errors": [f"board not found: {board}"], "dry_run": dry_run}
    monitored=[]; errors=[]
    conn=kb.connect(board=board)
    try:
        for row in _role_rows(conn, task_id=task_id):
            task=kb.Task.from_row(row); meta=_parse_role_body(task.body)
            if _reset_waiting_on_upstream(conn, task):
                monitored.append({"task_id": task.id, "action": "waiting_on_upstream", "status": "todo"})
                continue
            provider=meta.get("provider")
            if provider != "codex":
                monitored.append({"task_id": task.id, "provider": provider, "action": "skipped_non_codex"})
                continue
            bridge=_load_bridge_state(task.id)
            live=_codex_native_session_live(task.id, bridge.get("thread_id"))
            thread_id = live.get("thread_id") or bridge.get("thread_id")
            binding = _read_session_binding(thread_id)
            active_task_id = binding.get("active_task_id")
            if active_task_id and active_task_id != task.id:
                monitored.append({"task_id": task.id, "action": "skipped_session_bound_to_other_task", "active_task_id": active_task_id, "thread_id": thread_id})
                continue
            pending = _codex_live_pending_approval(thread_id)
            if pending.get("pending"):
                kind = pending.get("kind")
                is_complete = kind == "role_completed_waiting_complete"
                if task.status != "blocked":
                    if is_complete:
                        reason = "Native Codex role completed; waiting for human Complete."
                        comment = (
                            "Native Codex role completed; waiting for human Complete.\n"
                            f"summary: {pending.get('last_agent_message') or ''}\n"
                            f"session: {pending.get('session_file')}"
                        )
                        action = "marked_blocked_role_complete"
                    else:
                        reason = f"Native Codex session is waiting for approval: {pending.get('justification') or pending.get('cmd') or pending.get('call_id')}"
                        comment = (
                            "Native Codex approval required.\n"
                            f"command: {pending.get('cmd')}\n"
                            f"reason: {pending.get('justification')}\n"
                            f"session: {pending.get('session_file')}"
                        )
                        action = "marked_blocked_native_approval"
                    if not dry_run:
                        with kb.write_txn(conn):
                            _set_status(conn, task.id, "blocked", result=reason)
                            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)", (task.id, "kanban-agency", comment, int(time.time())))
                    monitored.append({"task_id": task.id, "action": action, "from_status": task.status, "pending": pending, "dry_run": dry_run})
                else:
                    monitored.append({"task_id": task.id, "action": "already_blocked_role_complete" if is_complete else "already_blocked_native_approval", "pending": pending})
            elif live.get("live"):
                if task.status != "running":
                    if not dry_run:
                        with kb.write_txn(conn):
                            next_result = task.result
                            if isinstance(next_result, str) and next_result.startswith("Native Codex session is waiting for approval:"):
                                next_result = "Native Codex session is live."
                            _set_status(conn, task.id, "running", result=next_result)
                            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)", (task.id, "kanban-agency", f"Native Codex session is live; marking task running.\nthread: {live.get('thread_id')}\nurl: {live.get('url')}", int(time.time())))
                    monitored.append({"task_id": task.id, "action": "marked_running_native_live", "from_status": task.status, "live": live, "dry_run": dry_run})
                else:
                    monitored.append({"task_id": task.id, "action": "already_running_native_live", "live": live})
            else:
                monitored.append({"task_id": task.id, "action": "no_native_session_live", "status": task.status, "bridge_state": bridge.get("state"), "live": live})
    finally:
        conn.close()
    return {"board": board, "monitored": monitored, "errors": errors, "dry_run": dry_run}

ROLE_SEQUENCE = ["analyst", "architect", "developer", "tester"]

def _role_title(role_key: str, root_title: str) -> str:
    labels = {
        "analyst": "clarify",
        "architect": "design",
        "developer": "implement",
        "tester": "verify",
    }
    verb = labels.get(role_key, "work on")
    return f"[agency] {role_key}: {verb} - {root_title}"


def _role_instruction(role_key: str, root_title: str) -> str:
    if role_key == "architect":
        return f"""架构设计师：需求分析阶段已经完成，请基于当前 root 的需求分析结果输出 `{root_title}` 的设计方案。

要求：
1. 先阅读 root/role comments、已有 spec 文档和相关仓库结构。
2. 明确范围、UI/接口/数据影响、风险和验收标准。
3. 输出可交给 developer 的设计结论。
4. 不要实现代码。"""
    if role_key == "developer":
        return f"""开发工程师：需求分析和设计已经完成，请实现 `{root_title}`。

要求：
1. 先阅读 root/role comments、需求/设计结论和相关仓库结构。
2. 按设计做最小可验证实现。
3. 补必要测试或 smoke 验证。
4. 不要提交 git commit；完成后输出实现摘要和验证命令结果，等待 tester。"""
    if role_key == "tester":
        return f"""测试工程师：开发阶段已经完成，请审查并测试 `{root_title}`。

要求：
1. 阅读需求/设计/开发输出。
2. 审查 git diff，确认实现范围和风险。
3. 运行必要的 lint/build/test/smoke 验证。
4. 输出测试结论、发现的问题、是否建议提交，以及建议排除的生成产物。
5. 不要提交 git commit。"""
    return f"请处理当前 root 任务：{root_title}"


def _role_rules_for(role_key: str) -> list[str]:
    try:
        roles, _warnings = load_roles(CONFIG_PATH)
        return list((roles.get(role_key) or Role(role_key, "codex", [], [])).rules)
    except Exception:
        return [f".ai/rules/{role_key}.md"]

def _make_role_card_body(root_id: str, role_key: str, provider: str, workdir: str, root_title: str, instruction: str) -> str:
    rules = _role_rules_for(role_key)
    rules_block = "\n".join(f"- {r}" for r in rules) if rules else "- (none)"
    return f"""@kanban-agency-role
root_id: {root_id}
role: {role_key}
provider: {provider}
workdir: {workdir}
root_title: {root_title}

rules:
{rules_block}

root_task_body:
```text
@kanban-agency
workdir: {workdir}

{instruction}
```

This is a kanban-agency role card. The root_task_body is the concrete active task.
"""

def _role_tasks_for_root(conn, root_id: str) -> dict[str, kb.Task]:
    rows = conn.execute("SELECT * FROM tasks WHERE title LIKE '[agency] %' AND body LIKE ? AND status != 'archived' ORDER BY created_at,id", (f"%root_id: {root_id}%",)).fetchall()
    out: dict[str, kb.Task] = {}
    for r in rows:
        t = kb.Task.from_row(r)
        role = _parse_role_body(t.body).get('role')
        if role and role not in out:
            out[role] = t
    return out


def ensure_workflow(conn, root: kb.Task, *, dry_run: bool = False) -> dict[str, Any]:
    """Ensure a functional-development root has analyst→architect→developer→tester role tasks precreated.

    The links are a dependency chain. Analyst may be force-promoted because root is
    an aggregate anchor and is not expected to complete before child roles.
    Later roles wait on the previous role and are started by run() only when ready.
    """
    workdir = _resolve_workdir(root.body or "", "")[0] or root.workspace_path or ""
    existing = _role_tasks_for_root(conn, root.id)
    created: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    previous_id: str | None = None
    for idx, role in enumerate(ROLE_SEQUENCE):
        if role in existing:
            reused.append({"role": role, "task_id": existing[role].id, "status": existing[role].status})
            previous_id = existing[role].id
            continue
        title = _role_title(role, root.title or root.id)
        body = _make_role_card_body(root.id, role, "codex", workdir, root.title or "", _role_instruction(role, root.title or root.id))
        parents = [previous_id] if previous_id else ([root.id] if idx == 0 else [])
        if dry_run:
            created.append({"role": role, "would_create": True, "title": title, "parents": parents})
            previous_id = f"<new-{role}>"
            continue
        task_id = kb.create_task(conn, title=title, body=body, assignee=_agency_assignee(role), created_by="kanban-agency", workspace_kind="dir" if workdir else "scratch", workspace_path=workdir or None, parents=[p for p in parents if p], initial_status="running")
        if idx == 0:
            try:
                kb.promote_task(conn, task_id, actor="kanban-agency", reason="start first workflow role", force=True)
            except Exception:
                pass
        created.append({"role": role, "task_id": task_id, "title": title, "parents": parents})
        previous_id = task_id
    return {"root_id": root.id, "created": created, "reused": reused}


def advance(board: str, root_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Ensure/precreate four-role workflow tasks, then start any ready role task.

    This is intentionally idempotent: all four role tasks are created once;
    subsequent calls only run role tasks whose dependency chain has made them
    ready. It retains compatibility with old roots by filling missing later roles.
    """
    if not board:
        return {"board": board, "advanced": [], "skipped": [], "errors": ["--board is required"], "dry_run": dry_run}
    if not kb.board_exists(board):
        return {"board": board, "advanced": [], "skipped": [], "errors": [f"board not found: {board}"], "dry_run": dry_run}
    advanced=[]; skipped=[]; errors=[]
    conn=kb.connect(board=board)
    try:
        if root_id:
            rows=conn.execute("SELECT * FROM tasks WHERE id=?", (root_id,)).fetchall()
        else:
            rows=conn.execute("SELECT * FROM tasks WHERE title NOT LIKE '[agency] %' AND body LIKE '%@kanban-agency%' AND status != 'archived' ORDER BY created_at,id").fetchall()
        for row in rows:
            root=kb.Task.from_row(row)
            wf=ensure_workflow(conn, root, dry_run=dry_run)
            ready_roles=[]
            if not dry_run:
                for role, task in _role_tasks_for_root(conn, root.id).items():
                    if task.status == 'ready':
                        ready_roles.append({"role": role, "task_id": task.id})
                if all((t.status in {'done','archived'}) for t in _role_tasks_for_root(conn, root.id).values()) and _role_tasks_for_root(conn, root.id):
                    with kb.write_txn(conn):
                        _set_status(conn, root.id, 'done', result='All workflow roles completed.')
            advanced.append({"root_id": root.id, "workflow": wf, "ready_roles": ready_roles})
    finally:
        conn.close()
    if not dry_run:
        try:
            run_result=run(board=board)
            for a in advanced:
                a["run_result"] = run_result
        except Exception as exc:
            errors.append(f"run after advance failed: {exc}")
    return {"board": board, "advanced": advanced, "skipped": skipped, "errors": errors, "dry_run": dry_run}

def workflow_watch(board: str, interval: float = 5.0, once: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Monitor sessions and auto-advance/precreate completed four-role workflows."""
    if not board:
        return {"board": board, "iterations": [], "errors": ["--board is required"], "dry_run": dry_run}
    iterations: list[dict[str, Any]] = []
    errors: list[str] = []
    while True:
        item: dict[str, Any] = {"at": int(time.time())}
        try:
            item["monitor"] = monitor(board, dry_run=dry_run)
        except Exception as exc:
            errors.append(f"monitor failed: {exc}")
            item["monitor_error"] = str(exc)
        try:
            item["advance"] = advance(board, dry_run=dry_run)
        except Exception as exc:
            errors.append(f"advance failed: {exc}")
            item["advance_error"] = str(exc)
        iterations.append(item)
        if once:
            break
        time.sleep(max(1.0, float(interval)))
    return {"board": board, "iterations": iterations, "errors": errors, "dry_run": dry_run}


def _ensure_task_body_link(conn, task_id: str, label: str, url: str) -> None:
    """Put a clickable provider session link on the first line of the role card body."""
    row = conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return
    body = row["body"] or ""
    link = f"[{label}]({url})"
    lines = body.splitlines()
    if lines and ("http://127.0.0.1:8766/s/" in lines[0] and ("href=" in lines[0] or lines[0].startswith("["))):
        lines[0] = link
        new_body = "\n".join(lines)
    else:
        new_body = link + ("\n" + body if body else "")
    if new_body != body:
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (new_body, task_id))

def ensure_claude_session_link(conn, board: str, task_id: str, session_id: str | None = None, cwd: str | None = None) -> dict[str, Any]:
    if not session_id:
        state = _read_claude_state(task_id)
        session_id = state.get("session_id")
        cwd = cwd or state.get("cwd")
    if not session_id:
        return {"ok": False, "reason": "no session_id"}
    url = claude_session_url(task_id)
    _ensure_task_body_link(conn, task_id, "Open Claude session", url)
    marker = f"Claude session link: {url}"
    comments = kb.list_comments(conn, task_id)
    if any(marker in (c.body or "") for c in comments):
        return {"ok": True, "url": url, "reused_comment": True}
    kb.add_comment(conn, task_id, author="kanban-agency", body=(
        f"Claude session link: {url}\n"
        f"task: {task_id}\nsession: {session_id}\ncwd: {cwd or ''}\n\n"
        "Open this link anytime to inspect or continue the native Claude session."
    ))
    return {"ok": True, "url": url, "reused_comment": False}



def _url_ok(url: str, timeout: float = 1.5) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= int(getattr(r, "status", 200)) < 500
    except Exception:
        return False

def claude_web(board: str | None, task_id: str, port: int | None = None, reuse: bool = True) -> dict[str, Any]:
    if not task_id:
        return {"ok": False, "error": "--task-id is required"}
    state = _read_claude_state(task_id)
    session_id = state.get("session_id")
    cwd = state.get("cwd")
    CLAUDE_WEB_DIR.mkdir(parents=True, exist_ok=True)
    state_path = _claude_web_state_path(task_id)
    existing = _read_json_file(state_path)
    existing_tmux = existing.get("tmux") or existing.get("tmux_name")
    existing_ttyd_ok = bool(existing.get("pid") and _pid_alive(existing.get("pid")) and existing.get("url") and _url_ok(str(existing.get("url"))))
    if reuse and existing_ttyd_ok and (not existing_tmux or _tmux_has_session(str(existing_tmux))):
        return {"ok": True, "reused": True, "url": existing.get("url"), "state": existing}
    if existing_ttyd_ok and existing_tmux and not _tmux_has_session(str(existing_tmux)):
        try:
            os.kill(int(existing.get("pid")), signal.SIGTERM)
        except Exception:
            pass
    if not session_id:
        tmux_name = state.get("tmux")
        if tmux_name and _tmux_has_session(str(tmux_name)) and cwd:
            use_port = int(port) if port else _free_port()
            url = f"http://127.0.0.1:{use_port}/"
            stdout_path = CLAUDE_WEB_DIR / f"{task_id}.stdout.log"
            stderr_path = CLAUDE_WEB_DIR / f"{task_id}.stderr.log"
            subprocess.run(["tmux", "set-option", "-t", str(tmux_name), "-g", "history-limit", "50000"], check=False)
            subprocess.run(["tmux", "set-option", "-t", str(tmux_name), "-g", "mouse", "off"], check=False)
            cmd = ["ttyd", "--interface", "127.0.0.1", "--port", str(use_port), "--writable", "-I", str(TTYD_WHEEL_INDEX), "-t", "scrollback=50000", "--client-option", f"titleFixed=Hermes {task.id}", "tmux", "attach-session", "-t", str(tmux_name)]
            out = stdout_path.open("ab"); err = stderr_path.open("ab")
            try:
                proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
            finally:
                out.close(); err.close()
            new_state = {"task_id": task_id, "board": board, "pid": proc.pid, "port": use_port, "url": url, "tmux": tmux_name, "cwd": str(cwd), "cmd": cmd, "stdout_log": str(stdout_path), "stderr_log": str(stderr_path), "started_at": int(time.time())}
            _write_json_file(state_path, new_state)
            return {"ok": True, "reused": False, "url": url, "state": new_state}
        return {"ok": False, "error": f"no claude session_id for task {task_id}; run first"}
    if not cwd:
        return {"ok": False, "error": f"no cwd in claude state for task {task_id}"}
    use_port = int(port) if port else _free_port()
    url = f"http://127.0.0.1:{use_port}/"
    stdout_path = CLAUDE_WEB_DIR / f"{task_id}.stdout.log"
    stderr_path = CLAUDE_WEB_DIR / f"{task_id}.stderr.log"
    cmd = [
        "ttyd", "--interface", "127.0.0.1", "--port", str(use_port), "--writable", "-I", str(TTYD_WHEEL_INDEX), "-t", "scrollback=50000", "--cwd", str(cwd),
        "bash", "-lc", f"exec claude --settings {Path.home() / '.hermes' / 'kanban-agency' / 'claude-ops-settings.json'} --resume {session_id}",
    ]
    out = stdout_path.open("ab"); err = stderr_path.open("ab")
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
    finally:
        out.close(); err.close()
    new_state = {"task_id": task_id, "board": board, "pid": proc.pid, "port": use_port, "url": url, "session_id": session_id, "cwd": str(cwd), "cmd": cmd, "stdout_log": str(stdout_path), "stderr_log": str(stderr_path), "started_at": int(time.time())}
    _write_json_file(state_path, new_state)
    if board and kb.board_exists(board):
        conn = kb.connect(board=board)
        try:
            fixed_url = codex_session_url(task_id)
            marker = f"Claude session link: {fixed_url}"
            if not any(marker in (c.body or "") for c in kb.list_comments(conn, task_id)):
                kb.add_comment(conn, task_id, author="kanban-agency", body=(
                    f"Claude session link: {fixed_url}\n"
                    f"task: {task_id}\nsession: {session_id}\ncwd: {cwd}\n\n"
                    "Open this link anytime to inspect or continue the native Claude session."
                ))
        finally:
            conn.close()
    return {"ok": True, "reused": False, "url": url, "state": new_state}

CODEX_WEB_GATEWAY_PORT = 8766
CODEX_WEB_GATEWAY_STATE = CODEX_WEB_DIR / "gateway.json"


def codex_session_url(task_id: str, port: int = CODEX_WEB_GATEWAY_PORT) -> str:
    return f"http://127.0.0.1:{int(port)}/s/{task_id}"


def ensure_codex_session_link(conn, board: str, task_id: str, thread_id: str | None = None, cwd: str | None = None) -> dict[str, Any]:
    if not thread_id:
        bridge = _load_bridge_state(task_id)
        thread_id = bridge.get("thread_id")
        cwd = cwd or bridge.get("cwd")
    if not thread_id:
        return {"ok": False, "reason": "no thread_id"}
    url = codex_session_url(task_id)
    _ensure_task_body_link(conn, task_id, "Open Codex session", url)
    marker = f"Codex session link: {url}"
    comments = kb.list_comments(conn, task_id)
    if any(marker in (c.body or "") for c in comments):
        return {"ok": True, "url": url, "reused_comment": True}
    kb.add_comment(conn, task_id, author="kanban-agency", body=(
        f"Codex session link: {url}\n"
        f"task: {task_id}\nthread: {thread_id}\ncwd: {cwd or ''}\n\n"
        "Open this link anytime to inspect or continue the native Codex session."
    ))
    return {"ok": True, "url": url, "reused_comment": False}







def tmux_scroll_task(task_id: str, delta: int = -800) -> dict[str, Any]:
    state = _read_json_file(_codex_web_state_path(task_id))
    if not state:
        state = _read_json_file(_claude_web_state_path(task_id))
    if not state:
        state = _read_json_file(_hermes_web_state_path(task_id))
    tmux_name = state.get("tmux_name") or state.get("tmux")
    if not tmux_name:
        return {"ok": False, "error": "no tmux session recorded", "task_id": task_id}
    tmux_name = str(tmux_name)
    if not _tmux_has_session(tmux_name):
        return {"ok": False, "error": "tmux session not alive", "task_id": task_id, "tmux_name": tmux_name}
    try:
        delta_i = int(delta)
    except Exception:
        delta_i = -800
    direction = "scroll-up" if delta_i < 0 else "scroll-down"
    steps = max(1, min(12, round(abs(delta_i) / 48) or 1))
    pane_in_mode = False
    scroll_position = None
    try:
        mode_raw = subprocess.check_output(["tmux", "display-message", "-p", "-t", tmux_name, "#{pane_in_mode}"], text=True, stderr=subprocess.STDOUT, env=_tmux_env()).strip()
        pane_in_mode = mode_raw == "1"
    except Exception:
        pane_in_mode = False
    # When already at the live bottom, downward wheel events should be cheap no-ops.
    # Entering/canceling tmux copy-mode on every tiny downward tick makes embedded
    # ttyd iframes feel sticky and weird.
    if direction == "scroll-down" and not pane_in_mode:
        return {"ok": True, "task_id": task_id, "tmux_name": tmux_name, "direction": direction, "steps": 0, "scroll_position": 0, "at_bottom": True}
    if not pane_in_mode:
        subprocess.run(["tmux", "copy-mode", "-e", "-t", tmux_name], check=False, env=_tmux_env())
    subprocess.run(["tmux", "send-keys", "-t", tmux_name, "-X", "-N", str(steps), direction], check=False, env=_tmux_env())
    if direction == "scroll-down":
        try:
            raw = subprocess.check_output(["tmux", "display-message", "-p", "-t", tmux_name, "#{scroll_position}"], text=True, stderr=subprocess.STDOUT, env=_tmux_env()).strip()
            scroll_position = int(raw or "0")
            if scroll_position <= 0:
                subprocess.run(["tmux", "send-keys", "-t", tmux_name, "-X", "cancel"], check=False, env=_tmux_env())
        except Exception:
            scroll_position = None
    return {"ok": True, "task_id": task_id, "tmux_name": tmux_name, "direction": direction, "steps": steps, "scroll_position": scroll_position, "at_bottom": scroll_position == 0 if scroll_position is not None else False}

def _tmux_capture_text(tmux_name: str, lines: int = 5000) -> str:
    try:
        return subprocess.check_output([
            "tmux", "capture-pane", "-p", "-J", "-S", f"-{int(lines)}", "-t", str(tmux_name)
        ], text=True, stderr=subprocess.STDOUT, env=_tmux_env())
    except Exception as exc:
        return f"[capture failed] {exc}"


def task_view_text(task_id: str) -> str:
    state = _read_json_file(_codex_web_state_path(task_id))
    tmux_name = state.get("tmux_name") or state.get("tmux")
    if not tmux_name:
        return "[no tmux session recorded]"
    if not _tmux_has_session(str(tmux_name)):
        return f"[tmux session stopped] {tmux_name}"
    return _tmux_capture_text(str(tmux_name))


def task_view_html(task_id: str) -> str:
    writable = codex_session_url(task_id)
    text = task_view_text(task_id)
    task_esc = html.escape(task_id)
    writable_esc = html.escape(writable)
    text_esc = html.escape(text)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>View {task_esc}</title><style>
*{{box-sizing:border-box}}html,body{{margin:0;width:100%;height:100%;background:#05070a;color:#dbe3ea;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;overflow:hidden}}.bar{{height:28px;background:#111822;border-bottom:1px solid #26313d;padding:5px 8px;white-space:nowrap;overflow:hidden}}.bar a{{color:#93c5fd;text-decoration:none}}pre{{margin:0;padding:8px;white-space:pre-wrap;word-break:break-word;overflow:auto;height:calc(100vh - 28px);line-height:1.35}}
</style></head><body><div class="bar">readonly observer - <a href="{writable_esc}" target="_blank">Open writable</a></div><pre id="out">{text_esc}</pre><script>
let atBottom=true;const pre=document.getElementById('out');function nearBottom(){{return pre.scrollTop+pre.clientHeight>=pre.scrollHeight-8}}pre.addEventListener('scroll',()=>{{atBottom=nearBottom()}});async function poll(){{try{{const r=await fetch('/view-text/{task_esc}',{{cache:'no-store'}});const t=await r.text();if(pre.textContent!==t){{const stick=atBottom||nearBottom();pre.textContent=t;if(stick)pre.scrollTop=pre.scrollHeight;}}}}catch(e){{}}}}pre.scrollTop=pre.scrollHeight;setInterval(poll,2000);
</script></body></html>"""





def _claude_attention_status(task_id: str) -> dict[str, Any]:
    web = _read_json_file(_claude_web_state_path(task_id))
    tmux_name = web.get("tmux_name") or web.get("tmux")
    if not tmux_name:
        return {"pending": False, "reason": "no_tmux"}
    tmux_name = str(tmux_name)
    if not _tmux_has_session(tmux_name):
        return {"pending": False, "reason": "tmux_not_alive", "tmux_name": tmux_name}
    try:
        text = subprocess.check_output(["tmux", "capture-pane", "-t", tmux_name, "-p", "-S", "-80"], text=True, stderr=subprocess.STDOUT)
    except Exception as exc:
        return {"pending": False, "reason": f"capture_failed: {exc}", "tmux_name": tmux_name}
    tail = "\n".join(text.splitlines()[-12:])
    lower = tail.lower()
    permission_markers = [
        "do you want to proceed", "allow", "permission", "yes, i accept", "bypass permissions", "trust this folder"
    ]
    prompt_markers = ["❯", "? for shortcuts"]
    busy_markers = ["●", "⏺", "running", "reading", "writing", "churned"]
    if any(m in lower for m in permission_markers):
        return {"pending": True, "kind": "permission_prompt", "tmux_name": tmux_name, "tail": tail}
    if any(m in tail for m in prompt_markers):
        return {"pending": True, "kind": "waiting_for_input", "tmux_name": tmux_name, "tail": tail}
    return {"pending": False, "kind": "running_or_idle_no_prompt", "tmux_name": tmux_name, "tail": tail}


def _hermes_attention_status(task_id: str) -> dict[str, Any]:
    """Detect Hermes native TUI waiting-for-input/permission prompts from tmux.

    Hermes-backed role sessions run inside tmux and do not emit Codex JSONL
    approval events. Treat the idle composer prompt as attention so Cockpit can
    ring the bell when a Hermes/orchestrator session is waiting on the user.
    """
    web = _read_json_file(_hermes_web_state_path(task_id))
    tmux_name = web.get("tmux_name") or web.get("tmux")
    if not tmux_name:
        return {"pending": False, "reason": "no_tmux"}
    tmux_name = str(tmux_name)
    if not _tmux_has_session(tmux_name):
        return {"pending": False, "reason": "tmux_not_alive", "tmux_name": tmux_name}
    try:
        text = subprocess.check_output(["tmux", "capture-pane", "-t", tmux_name, "-p", "-S", "-80"], text=True, stderr=subprocess.STDOUT)
    except Exception as exc:
        return {"pending": False, "reason": f"capture_failed: {exc}", "tmux_name": tmux_name}
    tail = "\n".join(text.splitlines()[-12:])
    lower = tail.lower()
    permission_markers = [
        "do you want to proceed", "allow", "permission", "yes, i accept",
        "confirm", "approval", "approve", "是否继续", "确认"
    ]
    prompt_markers = ["❯", "? for shortcuts"]
    # Hermes shows an input-looking line with "msg=interrupt" while the agent is
    # busy and only accepting steering/cancel commands. That is not a user-input
    # bell; otherwise the bell turns on when work starts and never clears until
    # the next full-screen redraw.
    busy_markers = [
        "msg=interrupt", "compacting context", "preflight compression",
    ]
    if any(m in lower for m in permission_markers):
        return {"pending": True, "kind": "permission_prompt", "tmux_name": tmux_name, "tail": tail}
    if any(m in lower for m in busy_markers):
        return {"pending": False, "kind": "busy_interruptible", "tmux_name": tmux_name, "tail": tail}
    if any(m in tail for m in prompt_markers):
        return {"pending": True, "kind": "waiting_for_input", "tmux_name": tmux_name, "tail": tail}
    return {"pending": False, "kind": "running_or_idle_no_prompt", "tmux_name": tmux_name, "tail": tail}

def _claude_session_live(task_id: str) -> dict[str, Any]:
    web = _read_json_file(_claude_web_state_path(task_id))
    pid = web.get("pid")
    ttyd_alive = bool(pid and _pid_alive(pid))
    tmux_name = web.get("tmux_name") or web.get("tmux")
    tmux_alive = bool(tmux_name and _tmux_has_session(str(tmux_name)))
    return {"live": bool(tmux_alive), "ttyd_alive": ttyd_alive, "ttyd_pid": pid, "tmux_alive": tmux_alive, "tmux_name": tmux_name, "url": web.get("url"), "web_state": web}

def _hermes_session_live(task_id: str) -> dict[str, Any]:
    web = _read_json_file(_hermes_web_state_path(task_id))
    pid = web.get("pid")
    ttyd_alive = bool(pid and _pid_alive(pid))
    tmux_name = web.get("tmux_name") or web.get("tmux")
    tmux_alive = bool(tmux_name and _tmux_has_session(str(tmux_name)))
    return {"live": bool(tmux_alive), "ttyd_alive": ttyd_alive, "ttyd_pid": pid, "tmux_alive": tmux_alive, "tmux_name": tmux_name, "url": web.get("url"), "web_state": web}


def session_alert_status(board: str | None, task_id: str) -> dict[str, Any]:
    """Status payload used by the /s/<task_id> wrapper for tab alerts."""
    provider = None
    task_status = None
    task_title = None
    task_result = None
    task_completed_at = None
    if board and kb.board_exists(board):
        conn = kb.connect(board=board)
        try:
            row = conn.execute('SELECT title,status,result,body,completed_at FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row:
                task_title = row['title']
                task_status = row['status']
                task_result = row['result']
                task_completed_at = row['completed_at']
                provider = _parse_role_body(row['body']).get('provider')
        finally:
            conn.close()
    bridge = _load_bridge_state(task_id)
    if provider == 'claude':
        web = _read_json_file(_claude_web_state_path(task_id))
        thread_id = None
        live = _claude_session_live(task_id)
        pending = _claude_attention_status(task_id)
    elif provider == 'hermes':
        web = _read_json_file(_hermes_web_state_path(task_id))
        thread_id = None
        live = _hermes_session_live(task_id)
        pending = _hermes_attention_status(task_id)
    else:
        web = _read_json_file(_codex_web_state_path(task_id))
        thread_id = web.get('thread_id') or bridge.get('thread_id')
        if not thread_id and (provider in {None, 'codex'}):
            thread_id = _recover_codex_thread_for_task(task_id, web)
            if thread_id:
                web = _read_json_file(_codex_web_state_path(task_id))
        live = _codex_native_session_live_for_status(task_id, thread_id) if (provider in {None, 'codex'} or thread_id) else {"live": False}
        pending = _codex_live_pending_approval(thread_id) if thread_id else {"pending": False, "reason": "no_thread"}
    if _provider_pending_acknowledged(task_status, task_completed_at, web, pending):
        pending = {
            "pending": False,
            "reason": "completion_acknowledged",
            "completed_at": task_completed_at,
            "acknowledged_at": web.get("completion_acknowledged_at"),
            "provider_kind": pending.get("kind"),
        }
    return {
        "ok": True,
        "task_id": task_id,
        "board": board,
        "provider": provider,
        "title": task_title,
        "task_status": task_status,
        "result": task_result,
        "thread_id": thread_id,
        "live": bool(live.get('live')),
        "live_detail": live,
        "tmux_alive": bool(live.get('tmux_alive')),
        "tmux_name": live.get('tmux_name'),
        "ttyd_alive": bool(live.get('ttyd_alive')),
        "ttyd_url": web.get("url") or live.get("url"),
        "readonly_ttyd_url": web.get("readonly_url"),
        "pending_approval": bool(pending.get('pending')),
        "pending": pending,
    }




_AUTO_ADVANCE_LAST: dict[str, float] = {}
_AUTO_ADVANCE_INTERVAL_SECONDS = 5.0



def _safe_role_key(role: str) -> str:
    key = (role or "").strip().lower().replace("-", "_")
    if not ROLE_KEY_RE.match(key):
        raise ValueError(f"invalid role key: {role!r}")
    return key


def _role_workspace_state_path(board: str, role: str) -> Path:
    return ROLE_WORKSPACE_DIR / board / f"{_safe_role_key(role)}.json"


def _read_role_workspace_state(board: str, role: str) -> dict[str, Any]:
    return _read_json_file(_role_workspace_state_path(board, role))


def _write_role_workspace_state(board: str, role: str, data: dict[str, Any]) -> None:
    path = _role_workspace_state_path(board, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_file(path, data)


_AVAILABLE_ROLE_DEFS_CACHE: tuple[float, str, list[dict[str, Any]]] = (0.0, "", [])
_AVAILABLE_ROLE_DEFS_CACHE_TTL = 5.0
_ROLES_CONFIG_CACHE: tuple[float, Any] = (0.0, None)
_ROLES_CONFIG_CACHE_TTL = 5.0


def _load_roles_cached() -> Any:
    global _ROLES_CONFIG_CACHE
    now = time.time()
    ts, cached = _ROLES_CONFIG_CACHE
    if cached is not None and now - ts < _ROLES_CONFIG_CACHE_TTL:
        return cached
    roles, _ = load_roles(CONFIG_PATH)
    _ROLES_CONFIG_CACHE = (now, roles)
    return roles


def _available_role_defs(board: str) -> list[dict[str, Any]]:
    global _AVAILABLE_ROLE_DEFS_CACHE
    now = time.time()
    ts, cached_board, cached_roles = _AVAILABLE_ROLE_DEFS_CACHE
    if cached_board == str(board) and now - ts < _AVAILABLE_ROLE_DEFS_CACHE_TTL:
        return [dict(r) for r in cached_roles]
    keys: list[str] = []
    providers: dict[str, str] = {}
    titles: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    try:
        roles = _load_roles_cached()
        for key, role in roles.items():
            if key == "default":
                continue
            keys.append(key)
            providers[key] = role.provider if role.provider in {"codex", "claude", "hermes"} else "codex"
            titles[key] = role.title or key
            descriptions[key] = role.description or ROLE_DESCRIPTIONS.get(key, "")
    except Exception:
        pass
    for key in DEFAULT_INDEPENDENT_ROLES:
        if key not in keys:
            keys.append(key)
    out = []
    for key in keys:
        state_board = INDEPENDENT_ROLE_BOARD
        state = _sync_role_workspace_exit(state_board, key, _read_role_workspace_state(state_board, key)) if state_board else {}
        task_id = state.get("task_id")
        active = state.get("state") == "active" and _task_exists_for_workspace(state_board, task_id)
        out.append({
            "role": key,
            "board": state_board,
            "provider": providers.get(key, "hermes" if key == "orchestrator" else "codex"),
            "title": titles.get(key, key),
            "description": descriptions.get(key) or ROLE_DESCRIPTIONS.get(key, ""),
            "rules": list((roles.get(key).rules if 'roles' in locals() and key in roles else [])),
            "aliases": list((roles.get(key).aliases if 'roles' in locals() and key in roles else [])),
            "active": bool(active),
            "status": "active" if active else "idle",
            "task_id": task_id if active else None,
        })
    _AVAILABLE_ROLE_DEFS_CACHE = (now, str(board), [dict(r) for r in out])
    return out


def _ensure_independent_root(conn, board: str, workdir: str | None = None) -> str:
    row = conn.execute(
        "SELECT id FROM tasks WHERE title=? AND body LIKE '%@kanban-agency-independent-root%' AND status != 'archived' ORDER BY created_at LIMIT 1",
        ("Independent tasks",),
    ).fetchone()
    if row:
        return str(row["id"])
    return kb.create_task(
        conn,
        title="Independent tasks",
        body="@kanban-agency-independent-root\n\nRole-scoped independent chats live under this root.",
        created_by="kanban-agency",
        workspace_kind="dir" if workdir else "scratch",
        workspace_path=workdir or None,
        initial_status="running",
    )




def _default_independent_role_workdir(role: str, requested: str | None = None, board_default: str | None = None) -> str:
    """Resolve workdir for independent role chats.

    Ops is intentionally neutral: it should not inherit a product repo or the
    kanban-agency plugin repo just because the cockpit was opened there.
    """
    if requested:
        return str(Path(requested).expanduser())
    if role in {"ops", "operator"}:
        return str(Path(board_default or INDEPENDENT_ROLE_DEFAULT_WORKDIR).expanduser())
    if board_default:
        return str(Path(board_default).expanduser())
    return INDEPENDENT_ROLE_DEFAULT_WORKDIR

def _independent_role_instruction(role: str) -> str:
    return (
        f"这是一个独立 {role} 角色会话，不属于某个 feature workflow。\n"
        "请先用简短摘要说明你能帮助解决的问题，然后等待用户给出具体任务。\n"
        "用户没有显式 /exit 前，这个会话会被反复复用。\n"
        "如果用户显式 /exit，请结束当前会话；之后拖拽 role 会创建新会话。\n"
    )




def _summarize_user_question(text: str, limit: int = 34) -> str:
    clean = " ".join((text or "").strip().split())
    if not clean:
        return "等待输入"
    return clean if len(clean) <= limit else clean[:limit].rstrip() + "…"


def _first_user_question_for_thread(thread_id: str | None) -> str | None:
    if not thread_id:
        return None
    path = _find_codex_session_file(thread_id)
    if not path:
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        payload = obj.get("payload") or {}
        if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = str(payload.get("message") or payload.get("text") or payload.get("last_user_message") or "").strip()
            if text and text != "/exit":
                return text
    return None


def _clean_independent_title(title: str | None, role: str = "") -> str:
    text = str(title or "").strip()
    if text.startswith("[agency]"):
        text = re.sub(r"^\[agency\]\s+[^:]+:\s*", "", text).strip()
    if not text or text == "independent chat":
        return "空白会话"
    return text


def _summary_title_from_result(result: str | None) -> str | None:
    text = " ".join(str(result or "").strip().split())
    if not text or text == "independent role provider session exited":
        return None
    if text.startswith("Claude interactive tmux session started"):
        return "Claude 交互会话"
    if "文件抽屉" in text or "右侧工作区分栏" in text:
        return "文件抽屉改为右侧工作区分栏"
    if "submodule" in text or ".gitmodules" in text:
        return "修复 submodule/.gitmodules CI 问题"
    if "Hindsight" in text and "Hermes" in text:
        return "调研 Hermes 与 Hindsight 关系"
    return text[:34].rstrip() + ("…" if len(text) > 34 else "")


def _independent_role_display_title(task_id: str, role: str, task_title: str | None = None, result: str | None = None) -> str:
    # Keep independent-chat titles stable. Agent result / latest pending output is
    # deliberately NOT used here because it makes the left-side root title flicker
    # as the conversation evolves. If the task has a manual/non-placeholder title,
    # trust the Kanban DB title. Otherwise infer once from the first real user
    # message in the provider transcript; users can later rename it explicitly.
    cleaned = _clean_independent_title(task_title, role)
    if cleaned != "空白会话":
        return cleaned
    state = _read_json_file(_codex_web_state_path(task_id))
    thread_id = state.get("thread_id")
    if not thread_id:
        for p in (ROLE_WORKSPACE_DIR / INDEPENDENT_ROLE_BOARD).glob("*.json"):
            ws = _read_json_file(p)
            if ws.get("task_id") == task_id:
                thread_id = ws.get("thread_id")
                break
    text = _first_user_question_for_thread(thread_id)
    if text and not text.strip().startswith("你是一个"):
        return _summarize_user_question(text)
    return "空白会话"


def _thread_has_explicit_exit(thread_id: str | None) -> bool:
    if not thread_id:
        return False
    path = _find_codex_session_file(thread_id)
    if not path:
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return False
    for line in reversed(lines[-200:]):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        payload = obj.get("payload") or {}
        if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = str(payload.get("message") or payload.get("text") or payload.get("last_user_message") or "").strip()
            return text == "/exit"
    return False


def _task_exists_for_workspace(board: str, task_id: str | None) -> bool:
    if not task_id or not kb.board_exists(board):
        return False
    conn = kb.connect(board=board)
    try:
        cur = conn.execute("SELECT status FROM tasks WHERE id=? AND status != 'archived'", (task_id,))
        row = cur.fetchone() if hasattr(cur, "fetchone") else None
        return bool(row)
    finally:
        conn.close()


def mark_role_workspace_exited(board: str, role: str, reason: str = "explicit exit") -> dict[str, Any]:
    board = INDEPENDENT_ROLE_BOARD
    key = _safe_role_key(role)
    state = _read_role_workspace_state(board, key)
    state.update({"board": board, "role": key, "state": "exited", "exit_reason": reason, "exited_at": int(time.time()), "updated_at": int(time.time())})
    _write_role_workspace_state(board, key, state)
    return {"ok": True, "board": board, "role": key, "state": state}




def _role_workspace_provider_live(state: dict[str, Any]) -> bool:
    task_id = state.get("task_id")
    provider = state.get("provider") or "codex"
    if not task_id:
        return False
    if provider == "claude":
        return bool(_claude_session_live(str(task_id)).get("live"))
    if provider == "hermes":
        return bool(_hermes_session_live(str(task_id)).get("live"))
    return bool(_codex_native_session_live_for_status(str(task_id), state.get("thread_id")).get("live"))


def _mark_independent_workspace_task_done(board: str, task_id: str | None, reason: str) -> None:
    if not task_id or not kb.board_exists(board):
        return
    conn = kb.connect(board=board)
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row["status"] not in {"done", "archived"}:
            _set_status(conn, task_id, "done", result=reason)
            conn.commit()
    finally:
        conn.close()

def _sync_role_workspace_exit(board: str, role: str, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("state") == "active" and _thread_has_explicit_exit(state.get("thread_id")):
        state.update({"state": "exited", "exit_reason": "user /exit", "exited_at": int(time.time()), "updated_at": int(time.time())})
        _mark_independent_workspace_task_done(board, state.get("task_id"), "independent role session exited via /exit")
        _write_role_workspace_state(board, role, state)
    return state


def open_role_workspace(board: str, role: str, provider: str | None = None, workdir: str | None = None) -> dict[str, Any]:
    source_board = board
    board = INDEPENDENT_ROLE_BOARD
    if not kb.board_exists(board):
        kb.create_board(board, name=INDEPENDENT_ROLE_BOARD_NAME, description="Role-scoped independent chats for kanban-agency", default_workdir=INDEPENDENT_ROLE_DEFAULT_WORKDIR)
    key = _safe_role_key(role)
    state = _sync_role_workspace_exit(board, key, _read_role_workspace_state(board, key))
    if state.get("state") == "active" and _task_exists_for_workspace(board, state.get("task_id")):
        return {"ok": True, "reused": True, "board": board, "source_board": source_board, "role": key, "task_id": state.get("task_id"), "root_id": state.get("root_id"), "url": f"/s/{state.get('task_id')}", "state": state}

    meta = kb.read_board_metadata(board)
    resolved_workdir = _default_independent_role_workdir(key, workdir, str(meta.get("default_workdir") or "") or None)
    role_provider = provider or next((r.get("provider") for r in _available_role_defs(source_board or board) if r.get("role") == key), "codex") or "codex"
    if role_provider not in {"codex", "claude", "hermes"}:
        role_provider = "codex"
    conn = kb.connect(board=board)
    try:
        root_id = _ensure_independent_root(conn, board, resolved_workdir)
        title = f"[agency] {key}: independent chat"
        body = _make_role_card_body(root_id, key, role_provider, resolved_workdir, "independent chat", _independent_role_instruction(key))
        body += "\n@kanban-agency-independent\nsession_policy: reuse_until_exit\n"
        task_id = kb.create_task(
            conn,
            title=title,
            body=body,
            assignee=_agency_assignee(key),
            created_by="kanban-agency",
            parents=[root_id],
            initial_status="running",
            workspace_kind="dir" if resolved_workdir else "scratch",
            workspace_path=resolved_workdir or None,
        )
        try:
            kb.promote_task(conn, task_id, actor="kanban-agency", reason="open independent role workspace", force=True)
        except Exception:
            pass
    finally:
        conn.close()
    if role_provider == "codex":
        conn2 = kb.connect(board=board)
        try:
            task_obj = kb.Task.from_row(conn2.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        finally:
            conn2.close()
        run_result = codex_native_init_role_session(board, task_obj, _parse_role_body(body))
    else:
        # Claude does not currently have a safe prefill-without-submit path here;
        # keep the task initialized and waiting for the user to open/run explicitly.
        run_result = {"ok": True, "initialized_only": True}
    web_state = _read_json_file(_codex_web_state_path(task_id)) if role_provider == "codex" else _read_claude_state(task_id)
    new_state = {
        "board": board,
        "role": key,
        "provider": role_provider,
        "task_id": task_id,
        "root_id": root_id,
        "state": "active",
        "thread_id": web_state.get("thread_id") or web_state.get("session_id") or (run_result.get("state") or {}).get("thread_id"),
        "tmux_name": web_state.get("tmux_name") or (run_result.get("state") or {}).get("tmux_name"),
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    _write_role_workspace_state(board, key, new_state)
    return {"ok": True, "reused": False, "board": board, "source_board": source_board, "role": key, "task_id": task_id, "root_id": root_id, "url": f"/s/{task_id}", "run": run_result, "state": new_state}

def _auto_advance_board(board: str) -> dict[str, Any]:
    """Auto-start already-ready agency role sessions before cockpit renders.

    Human Complete lets Kanban recompute the child to ready. The next cockpit
    refresh starts only those already-ready role sessions. It deliberately does
    not call run(board) for every role, because that can rebuild existing running
    sessions; and it does not call advance(), because simply viewing Cockpit must
    not create missing workflow tasks.
    """
    now = float(time.time())
    last = _AUTO_ADVANCE_LAST.get(board, 0.0)
    if now - last < _AUTO_ADVANCE_INTERVAL_SECONDS:
        return {"ok": True, "skipped": "throttled"}
    _AUTO_ADVANCE_LAST[board] = now
    ready_ids: list[str] = []
    conn = kb.connect(board=board)
    try:
        for row in _role_rows(conn):
            task = kb.Task.from_row(row)
            meta = _parse_role_body(task.body)
            if task.status == "ready" and meta.get("provider") in {"codex", "claude", "hermes"}:
                ready_ids.append(task.id)
    finally:
        conn.close()
    results = []
    errors = []
    for tid in ready_ids:
        try:
            results.append(run(board=board, task_id=tid))
        except Exception as exc:
            errors.append(f"run {tid} failed: {exc}")
    return {"ok": not errors, "ready_task_ids": ready_ids, "runs": results, "errors": errors}

def create_board_api(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a kanban for Cockpit with a required project workdir."""
    km = _kanban_management_module()
    return km.create_kanban_api(kb=kb, board_exists=kb.board_exists, payload=payload)



def archive_board_api(payload: dict[str, Any]) -> dict[str, Any]:
    km = _kanban_management_module()
    return km.archive_kanban_api(kb=kb, protected_kanbans={"default", INDEPENDENT_ROLE_BOARD}, payload=payload)



def _editable_rule_path(rule: str) -> Path:
    rp = Path(str(rule or "").strip()).expanduser()
    if not rp.is_absolute():
        raise ValueError("rule path must be absolute to edit source")
    return rp


def role_rule_sources_api(role_key: str, path: Path = CONFIG_PATH) -> dict[str, Any]:
    role_key = str(role_key or "").strip()
    if not ROLE_KEY_RE.match(role_key) or role_key == "default":
        return {"ok": False, "error": "invalid role"}
    try:
        roles, _ = load_roles(path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    role = roles.get(role_key)
    if not role:
        return {"ok": False, "error": "role not found"}
    sources = []
    for rule in role.rules:
        item = {"path": rule, "content": "", "exists": False, "editable": False, "error": None}
        try:
            rp = _editable_rule_path(rule)
            item["editable"] = True
            item["exists"] = rp.exists()
            if rp.exists():
                item["content"] = rp.read_text(encoding="utf-8")
        except Exception as exc:
            item["error"] = str(exc)
        sources.append(item)
    return {"ok": True, "role": role_key, "rules": sources}


def update_role_config_api(payload: dict[str, Any], path: Path = CONFIG_PATH) -> dict[str, Any]:
    role_key = str(payload.get("role") or "").strip()
    if not ROLE_KEY_RE.match(role_key) or role_key == "default":
        return {"ok": False, "error": "invalid role"}
    provider = str(payload.get("provider") or "").strip().lower()
    if provider not in VALID_PROVIDERS:
        return {"ok": False, "error": "invalid provider"}
    title = str(payload.get("title") or role_key).strip() or role_key
    description = str(payload.get("description") or "").strip()
    rules_raw = payload.get("rules") or []
    aliases_raw = payload.get("aliases") or []
    if not isinstance(rules_raw, list) or not all(isinstance(x, str) for x in rules_raw):
        return {"ok": False, "error": "rules must be a string array"}
    if not isinstance(aliases_raw, list) or not all(isinstance(x, str) for x in aliases_raw):
        return {"ok": False, "error": "aliases must be a string array"}
    rules = [x.strip() for x in rules_raw if x.strip()]
    aliases = [x.strip() for x in aliases_raw if x.strip()]
    if yaml is None:
        return {"ok": False, "error": "PyYAML is required"}
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        data = {"roles": {"default": {"provider": "hermes"}}}
    if not isinstance(data, dict):
        return {"ok": False, "error": "roles.yaml root must be a mapping"}
    roles = data.setdefault("roles", {})
    if not isinstance(roles, dict):
        return {"ok": False, "error": "roles must be a mapping"}
    raw = roles.get(role_key) or {}
    if not isinstance(raw, dict):
        raw = {}
    raw["title"] = title
    raw["description"] = description
    raw["provider"] = provider
    raw["rules"] = rules
    raw["aliases"] = aliases
    roles[role_key] = raw
    if path.exists():
        backup = path.with_name(path.name + f".bak.{int(time.time())}")
        shutil.copy2(path, backup)
    else:
        backup = None
    rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    path.write_text(rendered, encoding="utf-8")
    # Validate by reloading after write.
    try:
        load_roles(path)
    except Exception as exc:
        return {"ok": False, "error": "saved roles.yaml is invalid: " + str(exc)}
    warnings = []
    rule_contents = payload.get("rule_contents") or {}
    if rule_contents and not isinstance(rule_contents, dict):
        return {"ok": False, "error": "rule_contents must be an object"}
    for rule in rules:
        try:
            rp = _editable_rule_path(rule)
        except Exception as exc:
            warnings.append(f"rule not editable: {rule}: {exc}")
            continue
        if rule in rule_contents:
            rp.parent.mkdir(parents=True, exist_ok=True)
            if rp.exists():
                shutil.copy2(rp, rp.with_name(rp.name + f".bak.{int(time.time())}"))
            rp.write_text(str(rule_contents[rule]), encoding="utf-8")
        if not rp.exists():
            warnings.append(f"rule file not found: {rule}")
    return {"ok": True, "role": role_key, "backup": str(backup) if backup else None, "warnings": warnings, "available_roles": _available_role_defs(INDEPENDENT_ROLE_BOARD)}


def create_task_api(board: str, payload: dict[str, Any]) -> dict[str, Any]:
    km = _kanban_management_module()
    return km.create_kanban_task_api(
        kb=kb,
        board=board,
        payload=payload,
        resolve_workdir=_resolve_workdir,
        advance=advance,
        available_role_defs=_available_role_defs,
        role_rules_for=_role_rules_for,
        agency_assignee=_agency_assignee,
    )



def update_task_title_api(task_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    task_id = (task_id or "").strip()
    payload = payload or {}
    title = str(payload.get("title") or "").strip()
    if not task_id:
        return {"ok": False, "error": "task_id is required"}
    if not title:
        return {"ok": False, "error": "title is required"}
    if len(title) > 160:
        return {"ok": False, "error": "title is too long (max 160 chars)"}
    board = str(payload.get("board") or "").strip() or _find_board_for_task(task_id)
    if not board or not kb.board_exists(board):
        return {"ok": False, "error": f"task not found: {task_id}"}
    conn = kb.connect(board=board)
    try:
        row = conn.execute("SELECT id,title,body FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"task not found: {task_id}"}
        old_title = row["title"] or ""
        meta = _parse_role_body(row["body"] or "")
        role = meta.get("role") or "session"
        # Independent role titles keep the standard Kanban role prefix while the
        # display title stays user-editable and synchronized to the DB.
        new_title = f"[agency] {role}: {title}" if old_title.startswith("[agency]") else title
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET title=? WHERE id=?", (new_title, task_id))
            try:
                kb._append_event(conn, task_id, "renamed", {"source": "cockpit", "from": old_title, "to": new_title})
            except Exception:
                pass
            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)", (task_id, "cockpit", f"Renamed from {old_title!r} to {new_title!r} via Cockpit."))
    finally:
        conn.close()
    return {"ok": True, "board": board, "task_id": task_id, "title": new_title, "display_title": title}


def reopen_task_api(task_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    board = str(payload.get("board") or "").strip() or _find_board_for_task(task_id)
    if not board or not kb.board_exists(board):
        return {"ok": False, "error": f"task not found: {task_id}"}
    status = str(payload.get("status") or "running").strip() or "running"
    if status not in {"running", "ready"}:
        return {"ok": False, "error": "reopen status must be running or ready"}
    conn = kb.connect(board=board)
    try:
        row = conn.execute('SELECT id,status,title FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"task not found: {task_id}"}
        old_status = row['status']
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=?, completed_at=NULL, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=?", (status, task_id))
            try:
                kb._append_event(conn, task_id, "reopened", {"source": "cockpit", "from": old_status, "to": status})
            except Exception:
                pass
            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)", (task_id, "cockpit", f"Reopened from {old_status} to {status} via Cockpit."))
    finally:
        conn.close()
    return {"ok": True, "board": board, "task_id": task_id, "status": status, "old_status": old_status}

def complete_task_api(task_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Complete a concrete Kanban task from Cockpit with provider summary."""
    task_id = (task_id or "").strip()
    if not task_id:
        return {"ok": False, "error": "task_id is required"}
    board = _find_board_for_task(task_id)
    if not board:
        return {"ok": False, "error": f"task not found: {task_id}"}
    payload = payload or {}
    status = session_alert_status(board, task_id)
    pending = status.get("pending") or {}
    comment = str(payload.get("comment") or pending.get("last_agent_message") or status.get("result") or "Completed from Cockpit.").strip()
    if not comment:
        comment = "Completed from Cockpit."
    conn = kb.connect(board=board)
    try:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"task not found: {task_id}"}
        if row["status"] == "done":
            state_path = _codex_web_state_path(task_id)
            state = _read_json_file(state_path)
            if state:
                state["completion_acknowledged_at"] = int(time.time())
                state["completion_comment"] = comment
                _write_json_file(state_path, state)
            return {"ok": True, "board": board, "task_id": task_id, "already_done": True, "comment": comment}
        kb.add_comment(conn, task_id, author="cockpit", body=comment)
        ok = kb.complete_task(conn, task_id, result=comment, summary=comment)
        if not ok:
            return {"ok": False, "error": f"task {task_id} could not be completed from status {row['status']}", "board": board, "task_id": task_id}
    finally:
        conn.close()
    state_path = _codex_web_state_path(task_id)
    state = _read_json_file(state_path)
    if state:
        state["completion_acknowledged_at"] = int(time.time())
        state["completion_comment"] = comment
        _write_json_file(state_path, state)
    return {"ok": True, "board": board, "task_id": task_id, "comment": comment}


def _file_mtime(path: Path) -> int:
    try:
        return int(path.stat().st_mtime) if path.exists() else 0
    except Exception:
        return 0


def _newest_file_mtime(root: Path, patterns: tuple[str, ...] = ("*.log", "*.json", "*.jsonl", "*.txt", "*.md"), limit: int = 200) -> int:
    if not root.exists():
        return 0
    best = 0
    seen = 0
    try:
        for pat in patterns:
            for f in root.rglob(pat):
                if not f.is_file():
                    continue
                seen += 1
                best = max(best, _file_mtime(f))
                if seen >= limit:
                    return best
    except Exception:
        return best
    return best


_PROVIDER_ACTIVITY_CACHE: dict[tuple[str, str, str], tuple[float, int]] = {}
_PROVIDER_ACTIVITY_CACHE_TTL = 2.0


def _provider_activity_at(task_id: str, provider: str | None = None, thread_id: str | None = None) -> int:
    provider = (provider or "").strip().lower()
    cache_key = (str(task_id), provider, str(thread_id or ""))
    now = time.time()
    cached = _PROVIDER_ACTIVITY_CACHE.get(cache_key)
    if cached and now - cached[0] < _PROVIDER_ACTIVITY_CACHE_TTL:
        return cached[1]
    vals: list[int] = []

    codex_state = _read_json_file(_codex_web_state_path(task_id))
    if not thread_id:
        thread_id = codex_state.get("thread_id") or codex_state.get("codex_thread_id")
    if not thread_id:
        bridge = _load_bridge_state(task_id)
        thread_id = bridge.get("thread_id") or bridge.get("codex_thread_id")
    if thread_id:
        session_file = _find_codex_session_file(str(thread_id))
        if session_file and session_file.exists():
            vals.append(_file_mtime(session_file))
    for key in ("updated_at", "last_activity_at", "started_at"):
        try:
            if codex_state.get(key):
                vals.append(int(float(codex_state.get(key))))
        except Exception:
            pass

    # Web wrapper logs are meaningful for providers without a structured transcript
    # (Hermes/Claude) and useful as a fallback for legacy Codex sessions.
    for base in (CODEX_WEB_DIR, CLAUDE_WEB_DIR, HERMES_WEB_DIR):
        for suffix in ("stdout.log", "stderr.log"):
            vals.append(_file_mtime(base / f"{task_id}.{suffix}"))

    # Provider run directories may contain transcripts/logs even when the web
    # state file itself is static. This is especially important for Claude/Ops
    # and Hermes/orchestrator native TUI sessions.
    vals.append(_newest_file_mtime(CLAUDE_RUN_DIR / task_id))
    vals.append(_newest_file_mtime(Path.home() / ".hermes" / "kanban-agency" / "hermes-runs" / task_id))

    # State files are low-signal but still better than nothing for old sessions.
    for sp in (_claude_web_state_path(task_id), _claude_state_path(task_id), _hermes_web_state_path(task_id), _codex_web_state_path(task_id)):
        vals.append(_file_mtime(sp))

    result = max(vals or [0])
    _PROVIDER_ACTIVITY_CACHE[cache_key] = (now, result)
    return result


def sessions_status(board: str) -> dict[str, Any]:
    if not board or not kb.board_exists(board):
        return {"ok": False, "board": board, "roots": [], "error": f"board not found: {board}"}
    auto_advance = _auto_advance_board(board)
    conn = kb.connect(board=board)
    try:
        root_rows = conn.execute("SELECT * FROM tasks WHERE title NOT LIKE '[agency] %' AND status != 'archived' ORDER BY created_at DESC,id DESC").fetchall()
        roots = []
        def task_changed_at(task_id: str, fallback: int | None = None) -> int:
            return task_recent_activity_at(conn, task_id, fallback)

        def root_changed_at(root_id: str, roles: list[dict[str, Any]], fallback: int | None = None) -> int:
            vals = [task_changed_at(root_id, fallback)]
            vals.extend(int(r.get("changed_at") or r.get("created_at") or 0) for r in roles if r.get("task_id"))
            return max(vals)

        def role_item(row):
            task = kb.Task.from_row(row)
            meta = _parse_role_body(task.body)
            role = meta.get('role') or 'unknown'
            st = session_alert_status(board, task.id)
            parents = [dict(r) for r in conn.execute("SELECT p.id,p.title,p.status FROM task_links l JOIN tasks p ON p.id=l.parent_id WHERE l.child_id=?", (task.id,)).fetchall()]
            is_independent_session = "@kanban-agency-independent" in (task.body or "")
            display_status = task.status
            if is_independent_session and task.status == "running" and not bool(st.get("live")):
                display_status = "done"
            pending_payload = st.get('pending')
            is_attention = display_status == "blocked" or bool(st.get('pending_approval'))
            if task.status == "blocked" and (not pending_payload or not pending_payload.get("pending")):
                pending_payload = {"pending": True, "kind": "blocked", "reason": task.result}
            display_title = _independent_role_display_title(task.id, role, task.title, task.result) if "@kanban-agency-independent" in (task.body or "") else (task.title or "")
            return {"role":role,"task_id":task.id,"title":task.title,"display_title":display_title,"independent":is_independent_session,"task_status":display_status,"result":task.result,"assignee":task.assignee,"created_at":task.created_at,"changed_at":task_changed_at(task.id, task.created_at),"url":f"/s/{task.id}","pending_approval":is_attention,"live":bool(st.get('live')),"tmux_alive":bool(st.get('tmux_alive')),"ttyd_alive":bool(st.get('ttyd_alive')),"thread_id":st.get('thread_id'),"ttyd_url":st.get('ttyd_url'),"readonly_ttyd_url":st.get('readonly_ttyd_url'),"has_session":bool(st.get('thread_id') or st.get('ttyd_url') or st.get('tmux_name')),"pending":pending_payload,"parents":parents,"parents_satisfied":_parents_satisfied(conn, task.id)}

        if board == INDEPENDENT_ROLE_BOARD:
            rows = conn.execute("SELECT * FROM tasks WHERE title LIKE '[agency] %' AND status != 'archived' AND body LIKE '%@kanban-agency-independent%' ORDER BY created_at DESC,id DESC").fetchall()
            for row in rows:
                item = role_item(row)
                roots.append({
                    "root_id": item.get("task_id"),
                    "title": item.get("display_title") or item.get("title") or "独立任务",
                    "status": item.get("task_status") or "running",
                    "collapsed": item.get("task_status") in {"done", "archived"},
                    "attention": 1 if item.get("pending_approval") else 0,
                    "changed_at": item.get("changed_at") or item.get("created_at"),
                    "roles": [item],
                    "independent": True,
                })
            return {"ok": True, "board": board, "roots": roots, "auto_advance": auto_advance, "available_roles": _available_role_defs(board)}

        grouped_task_ids = set()
        for rr in root_rows:
            root = kb.Task.from_row(rr)
            role_rows = conn.execute("SELECT * FROM tasks WHERE title LIKE '[agency] %' AND body LIKE ? AND status != 'archived' ORDER BY created_at,id", (f"%root_id: {root.id}%",)).fetchall()
            roles=[]; seen=set()
            for row in role_rows:
                item = role_item(row)
                roles.append(item)
                seen.add(item.get('role'))
                grouped_task_ids.add(item.get('task_id'))
            is_independent_root = (root.title or "") == "Independent tasks" or "@kanban-agency-independent-root" in (root.body or "")
            for role in ROLE_SEQUENCE:
                if not is_independent_root and role not in seen:
                    roles.append({"role":role,"task_id":None,"title":f"{role} not created","task_status":"missing","url":None,"pending_approval":False,"live":False,"parents":[],"parents_satisfied":False})
            if is_independent_root:
                roles.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
            else:
                roles.sort(key=lambda r: ROLE_SEQUENCE.index(r.get("role")) if r.get("role") in ROLE_SEQUENCE else len(ROLE_SEQUENCE))
            
            real_roles = [r for r in roles if r.get('task_id')]
            collapsed = bool(real_roles) and (is_independent_root or all(r.get('task_status') in {'done','archived'} for r in real_roles))
            roots.append({"root_id":root.id,"title":root.title,"status":root.status,"collapsed":collapsed,"attention":sum(1 for r in roles if r.get('pending_approval')),"changed_at":root_changed_at(root.id, roles, root.created_at),"roles":roles})

        independent_rows = conn.execute("SELECT * FROM tasks WHERE title LIKE '[agency] %' AND status != 'archived' AND (body NOT LIKE '%root_id:%' OR body IS NULL) ORDER BY created_at DESC,id DESC").fetchall()
        orphan_roles = []
        for row in independent_rows:
            item = role_item(row)
            if item.get('task_id') in grouped_task_ids:
                continue
            if item.get("independent"):
                roots.append({
                    "root_id": item.get("task_id"),
                    "title": item.get("display_title") or item.get("title") or "独立任务",
                    "status": item.get("task_status") or "running",
                    "collapsed": item.get("task_status") in {"done", "archived"},
                    "attention": 1 if item.get("pending_approval") else 0,
                    "changed_at": item.get("changed_at") or item.get("created_at"),
                    "roles": [item],
                    "independent": True,
                })
            else:
                orphan_roles.append(item)
        if orphan_roles:
            roots.append({"root_id":"__independent__","title":"Independent tasks","status":"running","collapsed":True,"attention":sum(1 for r in orphan_roles if r.get('pending_approval')),"changed_at":max([int(r.get("changed_at") or r.get("created_at") or 0) for r in orphan_roles] or [0]),"roles":orphan_roles})
        return {"ok": True, "board": board, "roots": roots, "auto_advance": auto_advance, "available_roles": _available_role_defs(board)}
    finally:
        conn.close()




def task_recent_activity_at(conn, task_id: str, fallback: int | None = None) -> int:
    """Return the same activity timestamp Cockpit Recent uses for a task."""
    vals = [int(fallback or 0)]
    try:
        row = conn.execute("SELECT created_at,started_at,completed_at FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row:
            vals.extend(int(row[k] or 0) for k in ("created_at", "started_at", "completed_at"))
    except Exception:
        pass
    try:
        c = conn.execute("SELECT MAX(created_at) AS ts FROM task_comments WHERE task_id=?", (task_id,)).fetchone()
        vals.append(int((c and c["ts"]) or 0))
    except Exception:
        pass
    try:
        e = conn.execute("SELECT MAX(created_at) AS ts FROM task_events WHERE task_id=? AND kind NOT IN ('stale','heartbeat')", (task_id,)).fetchone()
        vals.append(int((e and e["ts"]) or 0))
    except Exception:
        pass
    try:
        row = conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        meta = _parse_role_body((row and row["body"]) or "") if row else {}
        vals.append(_provider_activity_at(task_id, meta.get("provider"), None))
    except Exception:
        pass
    return max(vals)


def cleanup_completed_sessions(max_age_days: int = 3, *, dry_run: bool = False, now: int | None = None) -> dict[str, Any]:
    """Stop tmux/ttyd sessions for role tasks completed longer than max_age_days.

    Only provider process state is stopped. Kanban tasks, comments, results, and
    provider thread/session ids remain in state files so /resume can recreate the
    TUI if someone needs to inspect the old session later.
    """
    now_i = int(now or time.time())
    cutoff = now_i - max(0, int(max_age_days)) * 86400
    stopped: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    for b in kb.list_boards(include_archived=False):
        board = b.get("slug")
        if not board or not kb.board_exists(board):
            continue
        try:
            conn = kb.connect(board=board)
        except Exception as exc:
            errors.append(f"{board}: {exc}")
            continue
        try:
            rows = conn.execute(
                "SELECT id,title,status,created_at,started_at,completed_at FROM tasks "
                "WHERE status IN ('done','archived') AND completed_at IS NOT NULL"
            ).fetchall()
            candidates = []
            for row in rows:
                changed_at = task_recent_activity_at(conn, str(row["id"]), row["created_at"])
                if changed_at <= cutoff:
                    candidates.append((row, changed_at))
        finally:
            conn.close()
        for row, changed_at in candidates:
            task_id = str(row["id"])
            states = [_read_json_file(p) for p in _state_paths_for_task(task_id) if p.exists()]
            if not states:
                skipped.append({"board": board, "task_id": task_id, "reason": "no provider state"})
                continue
            tmux_alive = any(_tmux_has_session(str(s.get("tmux_name") or s.get("tmux"))) for s in states if (s.get("tmux_name") or s.get("tmux")))
            ttyd_alive = any(_pid_alive(s.get("pid")) or _pid_alive(s.get("readonly_pid")) for s in states)
            if not tmux_alive and not ttyd_alive:
                skipped.append({"board": board, "task_id": task_id, "reason": "already stopped"})
                continue
            try:
                data = _stop_provider_session_state(task_id, reason=f"completed>{max_age_days}d", now=now_i, dry_run=dry_run)
                data.update({"board": board, "title": row["title"], "completed_at": row["completed_at"], "changed_at": changed_at})
                stopped.append(data)
            except Exception as exc:
                errors.append(f"{board}/{task_id}: {exc}")
    return {"ok": not errors, "max_age_days": max_age_days, "cutoff": cutoff, "dry_run": dry_run, "stopped": stopped, "skipped_count": len(skipped), "errors": errors}


_COMPLETED_SESSION_CLEANUP_LAST = 0.0
_COMPLETED_SESSION_CLEANUP_INTERVAL_SECONDS = 3600.0

def _auto_cleanup_completed_sessions() -> dict[str, Any]:
    global _COMPLETED_SESSION_CLEANUP_LAST
    now = time.time()
    if now - _COMPLETED_SESSION_CLEANUP_LAST < _COMPLETED_SESSION_CLEANUP_INTERVAL_SECONDS:
        return {"ok": True, "skipped": "throttled"}
    _COMPLETED_SESSION_CLEANUP_LAST = now
    return cleanup_completed_sessions(max_age_days=3, dry_run=False, now=int(now))


def sessions_all() -> dict[str, Any]:
    cleanup = _auto_cleanup_completed_sessions()
    roots: list[dict[str, Any]] = []
    boards = []
    for b in sorted(kb.list_boards(), key=lambda x: x.get('created_at') or 0, reverse=True):
        if b.get("archived"):
            continue
        slug = b.get("slug")
        if not slug or not kb.board_exists(slug):
            continue
        board_title = b.get("name") or slug
        try:
            data = sessions_status(slug)
        except Exception as exc:
            boards.append({"board": slug, "title": board_title, "root_count": 0, "error": str(exc)})
            roots.append({
                "root_id": f"board:{slug}",
                "board": slug,
                "board_title": board_title,
                "title": board_title,
                "status": "error",
                "collapsed": False,
                "attention": 0,
                "roles": [],
                "empty_board": True,
                "error": str(exc),
                "default_workdir": b.get("default_workdir"),
            })
            continue
        if not data.get("ok"):
            boards.append({"board": slug, "title": board_title, "root_count": 0, "error": data.get("error")})
            continue
        active_roots = []
        for root in data.get("roots") or []:
            roles = root.get("roles") or []
            # Keep roots that have a real role task. This excludes empty smoke boards.
            if not any(r.get("task_id") for r in roles):
                continue
            root = dict(root)
            root["board"] = slug
            root["board_title"] = board_title
            root["title"] = f"{board_title} / {root.get('title') or ''}"
            for r in roles:
                if isinstance(r, dict):
                    r["board"] = slug
                    r["board_title"] = board_title
            active_roots.append(root)
        if active_roots:
            boards.append({"board": slug, "title": board_title, "root_count": len(active_roots)})
            roots.extend(active_roots)
        else:
            boards.append({"board": slug, "title": board_title, "root_count": 0})
            roots.append({
                "root_id": f"board:{slug}",
                "board": slug,
                "board_title": board_title,
                "title": board_title,
                "status": "empty",
                "collapsed": False,
                "attention": 0,
                "roles": [],
                "empty_board": True,
                "default_workdir": b.get("default_workdir"),
            })
    primary_board = None
    for root in roots:
        if root.get("attention"):
            primary_board = root.get("board")
            break
    if not primary_board and roots:
        primary_board = roots[0].get("board")
    return {"ok": True, "board": "__all__", "boards": boards, "roots": roots, "available_roles": _available_role_defs(INDEPENDENT_ROLE_BOARD), "completed_session_cleanup": cleanup}

def _cockpit_html(board: str, embed: bool = False) -> str:
    html = r"""<!doctype html><html><head><meta charset="utf-8"><title>Session Cockpit</title><style>
*{box-sizing:border-box}html,body{width:100%;height:100%}body{margin:0;background:#0b0f14;color:#dbe3ea;font:13px system-ui,sans-serif;overflow:hidden}.app{display:grid;grid-template-columns:220px minmax(0,1fr);width:100vw;height:100vh;overflow:hidden}.side{border-right:1px solid #26313d;background:#111822;overflow:hidden;padding:10px;display:grid;grid-template-rows:auto minmax(0,1fr) auto;min-height:0}#sessions{min-height:0;overflow:auto;padding-right:.15rem;scrollbar-width:none;-ms-overflow-style:none}#sessions::-webkit-scrollbar{width:0;height:0;display:none}.main{display:grid;grid-template-rows:40px minmax(0,1fr);min-width:0;width:100%;height:100vh;overflow:hidden}.side-tabs{display:flex;gap:6px;margin-bottom:8px}.sideTab{flex:1;background:#17202b;color:#dbe3ea;border:1px solid #2d3a49;border-radius:5px;padding:3px 7px;cursor:pointer}.sideTab.active{background:#1b3553;border-color:#60a5fa}.top{height:40px;min-height:40px;max-height:40px;overflow:hidden;padding:8px 12px;border-bottom:1px solid #26313d;background:#111822;display:flex;gap:8px;align-items:center;flex-wrap:nowrap}.layoutBtn{background:#17202b;color:#dbe3ea;border:1px solid #2d3a49;border-radius:5px;padding:3px 7px;cursor:pointer}.layoutBtn.active{background:#1b3553;border-color:#60a5fa}.panes{display:grid;min-height:0;width:100%;height:100%;overflow:hidden;gap:0}.layout-1{grid-template-columns:1fr;grid-template-rows:1fr}.layout-2{grid-template-columns:minmax(0,1fr) minmax(0,2fr);grid-template-rows:1fr}.layout-3{grid-template-columns:repeat(3,minmax(0,1fr));grid-template-rows:1fr}.layout-2x2{grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:repeat(2,minmax(0,1fr))}.layout-3x2{grid-template-columns:repeat(3,minmax(0,1fr));grid-template-rows:repeat(2,minmax(0,1fr))}.layout-left-split{grid-template-columns:1fr 1fr 1fr;grid-template-rows:1fr 1fr}.layout-left-split .pane:nth-child(1){grid-row:1}.layout-left-split .pane:nth-child(2){grid-column:1;grid-row:2}.layout-left-split .pane:nth-child(3){grid-column:2;grid-row:1/3}.layout-left-split .pane:nth-child(4){grid-column:3;grid-row:1/3}.layout-main-side{grid-template-columns:2fr 1fr;grid-template-rows:1fr 1fr}.layout-main-side .pane:nth-child(1){grid-row:1/3}.layout-main-side .pane:nth-child(2){grid-column:2;grid-row:1}.layout-main-side .pane:nth-child(3){grid-column:2;grid-row:2}.pane{position:relative;border-right:1px solid #26313d;border-bottom:1px solid #26313d;display:grid;grid-template-rows:32px minmax(0,1fr);min-width:0;min-height:0;overflow:hidden;user-select:text}body.dragging .body iframe{pointer-events:none}.pane.active .ph{background:#1b3553}.pane.dropTarget{outline:2px solid #60a5fa;outline-offset:-2px}.ph{height:32px;line-height:18px;padding:7px 8px;border-bottom:1px solid #26313d;background:#151f2b;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.pane-id{color:#60a5fa;font-weight:700;margin-right:6px}.ph a{float:right;color:#93c5fd;text-decoration:none;font-size:11px}.ph a:hover{text-decoration:underline}.body{min-height:0;width:100%;height:100%;background:#05070a;overflow:hidden}.body iframe{display:block;width:100%;height:100%;border:0}.board-group{margin:8px 0 14px}.recent-workset{margin:0 0 16px;padding:8px 8px 10px;border:1px solid #26313d;border-radius:9px;background:#2d1b3d}.kanbans-group{border-top:2px solid #334155;padding-top:10px}.board-title{font-size:12px;letter-spacing:.03em;text-transform:uppercase;color:#93c5fd;font-weight:800;margin:10px 0 5px}.root{margin:5px 0 8px}.root-title{font-weight:700;color:#e5e7eb;margin:4px 0;cursor:pointer;user-select:none;padding:5px 7px;border-radius:7px;background:#16202c;border-left:3px solid #334155;display:flex;align-items:center;gap:6px}.root-title.open{border-left-color:#60a5fa;background:#18283a}.root-title.closed{opacity:.75}.root-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.root-state{font-size:11px;color:#94a3b8}.st{display:inline-block;width:1.15em;margin-right:4px;font-weight:800;text-align:center}.st-attention{color:#f59e0b}.st-blocked{color:#fb7185}.st-running{color:#38bdf8}.st-ready{color:#a78bfa}.st-review{color:#fbbf24}.st-todo{color:#94a3b8}.st-done{color:#22c55e}.st-idle{color:#64748b}.st-missing{color:#475569}.chip{display:block;width:100%;text-align:left;margin:3px 0;padding:4px 7px 4px 12px;border:1px solid #26313d;border-radius:6px;background:#121b26;color:#dbe3ea;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.chip.role-def{border-style:dashed;background:#101622;color:#b8c7d6}.chip.role-def.active{border-color:#22c55e;color:#bbf7d0}.chip.role-def.idle{border-color:#475569;color:#94a3b8}.role-card{margin:.38em 0 .62em;padding:.38em .54em;border:1px solid #334155;border-radius:.54em;background:#101827;cursor:pointer;font-size:1em}.role-card:hover{background:#172033;border-color:#60a5fa}.role-card-head{display:flex;align-items:center;justify-content:space-between;gap:.46em}.role-title{display:flex;align-items:center;gap:.46em;min-width:0}.role-logo{display:inline-flex;align-items:center;justify-content:center;inline-size:1.35em;block-size:1.35em;border-radius:.38em;font-size:.85em;font-weight:900;flex:0 0 auto}.role-name{font-weight:700;color:#e5e7eb;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.role-provider{font-size:.85em;text-transform:uppercase;border:1px solid #334155;border-radius:999px;padding:0 .46em;line-height:1.35}.provider-codex .role-logo,.role-modal.provider-codex .role-logo{background:#10233f;color:#bfdbfe}.provider-codex .role-provider{color:#bfdbfe;border-color:#3b82f6}.provider-claude .role-logo,.role-modal.provider-claude .role-logo{background:#2d1b3d;color:#e9d5ff}.provider-claude .role-provider{color:#e9d5ff;border-color:#a855f7}.provider-hermes .role-logo,.role-modal.provider-hermes .role-logo{background:#0f2f2b;color:#99f6e4}.provider-hermes .role-provider{color:#99f6e4;border-color:#14b8a6}.provider-human .role-logo,.role-modal.provider-human .role-logo{background:#3a2a10;color:#fde68a}.provider-human .role-provider{color:#fde68a;border-color:#f59e0b}.role-desc{margin-top:.23em;color:#cbd5e1;font-size:1em;line-height:1.25;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden}.role-action{margin-top:.23em;color:#94a3b8;font-size:.92em;line-height:1.2}.role-modal h2{display:flex;align-items:center;gap:8px;margin:0 0 4px}.role-detail-grid{display:grid;grid-template-columns:90px 1fr;gap:6px 10px;margin-top:12px}.role-detail-label{color:#94a3b8}.role-list{margin:0;padding-left:18px}.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}.role-form label{display:block;margin-top:8px;color:#94a3b8;font-size:12px}.role-form input,.role-form textarea,.role-form select{width:100%;margin-top:4px;background:#0b0f14;color:#dbe3ea;border:1px solid #2d3a49;border-radius:6px;padding:7px}.role-form textarea{min-height:70px}.role-source{margin-top:8px;border:1px solid #26313d;border-radius:8px;padding:8px;background:#0b0f14}.role-source-path{font-size:11px;color:#93c5fd;margin-bottom:4px;word-break:break-all}.role-source textarea{min-height:180px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.role-form-msg{margin-top:8px;color:#94a3b8}.pane-ref{float:right;color:#60a5fa;font-weight:700}.pane-action{float:right;margin-left:8px;border:1px solid #334155;border-radius:6px;padding:1px 6px;background:#0f172a;font-size:11px;line-height:16px;cursor:pointer}.pane-action.complete{color:#bfdbfe;background:#10233f;border-color:#3b82f6}.pane-action.complete:hover{color:#eff6ff;background:#1e3a5f;border-color:#60a5fa}.pane-action.reopen{color:#bbf7d0;background:#0f2a1a;border-color:#22c55e}.pane-action.reopen:hover{color:#dcfce7;background:#14532d;border-color:#4ade80}.chip:hover{background:#223044}.chip.blocked{border-color:#f59e0b;color:#fde68a}.chip.running{border-color:#38bdf8}.chip.done{opacity:.65}.chip.todo,.chip.missing{opacity:.55}.placeholder{padding:14px;color:#94a3b8;line-height:1.5}.small{color:#94a3b8}.summary{white-space:pre-wrap;max-height:45vh;overflow:auto}.hiddenHead .top{display:none}
</style></head><body class="__EMBED__"><div class="app"><aside class="side"><div class="side-tabs"><button id="tabSessions" class="sideTab active" onclick="setSideMode('sessions')">Kanbans</button><button id="tabRoles" class="sideTab" onclick="setSideMode('roles')">Roles</button></div><div id="sessions"></div><div id="archiveDrop" class="small" style="margin-top:10px;padding:8px;border:1px dashed #475569;border-radius:7px;text-align:center;color:#94a3b8">Drag kanban here to archive</div></aside><main class="main"><div class="top"><strong>Session Cockpit</strong><span id="attention" class="small"></span><span class="small">Layout:</span><span id="layouts"></span><span class="small">drag a role or session into any pane</span></div><section class="panes layout-3" id="panes"></section></main></div><div id="taskDialog" style="display:none;position:fixed;inset:0;background:#0009;z-index:9999;align-items:center;justify-content:center"><div style="width:420px;background:#111822;border:1px solid #2d3a49;border-radius:10px;padding:14px;box-shadow:0 12px 40px #000"><h3 style="margin:0 0 10px">New task</h3><label class="small">Kanban</label><select id="newTaskBoard" style="width:100%;margin:4px 0 10px;background:#0b0f14;color:#dbe3ea;border:1px solid #2d3a49;border-radius:6px;padding:7px"></select><label class="small">Title</label><input id="newTaskTitle" style="width:100%;margin:4px 0 10px;background:#0b0f14;color:#dbe3ea;border:1px solid #2d3a49;border-radius:6px;padding:7px" placeholder="task title"><label class="small">Task type</label><select id="newTaskMode" onchange="syncTaskRoleVisibility()" style="width:100%;margin:4px 0 10px;background:#0b0f14;color:#dbe3ea;border:1px solid #2d3a49;border-radius:6px;padding:7px"><option value="workflow">Four-role workflow</option><option value="independent">Independent role task</option></select><div id="taskRoleWrap" style="display:none"><label class="small">Role</label><select id="newTaskRole" style="width:100%;margin:4px 0 10px;background:#0b0f14;color:#dbe3ea;border:1px solid #2d3a49;border-radius:6px;padding:7px"><option value="analyst">analyst</option><option value="architect">architect</option><option value="developer">developer</option><option value="tester">tester</option><option value="ops">ops</option><option value="assistant">assistant</option></select></div><label class="small">Description</label><textarea id="newTaskBody" style="width:100%;height:80px;margin:4px 0 10px;background:#0b0f14;color:#dbe3ea;border:1px solid #2d3a49;border-radius:6px;padding:7px" placeholder="optional details"></textarea><div id="taskMsg" class="small" style="min-height:18px"></div><div style="display:flex;justify-content:flex-end;gap:8px;margin-top:10px"><button class="layoutBtn" onclick="hideTaskDialog()">Cancel</button><button class="layoutBtn" onclick="createTask()">Create</button></div></div></div><div id="genericModal" style="display:none;position:fixed;inset:0;background:#0009;z-index:10000;align-items:center;justify-content:center" onclick="if(event.target===this)closeModal()"><div id="genericModalBody" style="width:min(620px,92vw);max-height:86vh;overflow:auto;background:#111822;border:1px solid #2d3a49;border-radius:10px;padding:14px;box-shadow:0 12px 40px #000"></div></div><script>
const storageKey='kanban-cockpit-state';let sessions={roots:[]};let sideMode='sessions';let layout='3';let paneCount=3;let panes=[null,null,null,null,null,null];let active=0;let expandedRoots=new Set();let collapsedRoots=new Set();let collapsedKanbans=new Set();let recentTasks={};let roleRuleSources={};let lastSideHtml='';let panesRenderedWithData=false;function saveState(){try{localStorage.setItem(storageKey,JSON.stringify({layout,sideMode,panes,active,expanded:[...expandedRoots],collapsedRoots:[...collapsedRoots],collapsedKanbans:[...collapsedKanbans],recentTasks}))}catch(e){}}function loadState(){try{const s=JSON.parse(localStorage.getItem(storageKey)||'{}');if(Array.isArray(s.panes))panes=s.panes.slice(0,6).concat([null,null,null,null,null,null]).slice(0,6);if(s.layout)layout=s.layout;if(s.sideMode)sideMode=s.sideMode;if(Number.isInteger(s.active))active=s.active;if(Array.isArray(s.expanded))expandedRoots=new Set(s.expanded);if(Array.isArray(s.collapsedRoots))collapsedRoots=new Set(s.collapsedRoots);if(Array.isArray(s.collapsedKanbans))collapsedKanbans=new Set(s.collapsedKanbans);recentTasks=(s.recentTasks&&typeof s.recentTasks==='object')?s.recentTasks:{}}catch(e){recentTasks={}}}
function syncTaskRoleVisibility(){const wrap=document.getElementById('taskRoleWrap');if(wrap)wrap.style.display=(document.getElementById('newTaskMode')?.value==='independent')?'block':'none'}
function boardOptionsHtml(selected){return (sessions.boards||[]).map(b=>`<option value="${esc(b.board)}" ${b.board===selected?'selected':''}>${esc(b.title||b.board)}</option>`).join('')}
function showTaskDialog(b){const el=document.getElementById('taskDialog');const sel=document.getElementById('newTaskBoard');if(sel)sel.innerHTML=boardOptionsHtml(b||'');if(el)el.style.display='flex';syncTaskRoleVisibility();setTimeout(()=>document.getElementById('newTaskTitle')?.focus(),0)}
function hideTaskDialog(){const el=document.getElementById('taskDialog');if(el)el.style.display='none'}
function slugifyBoardName(name){return String(name||'').trim().toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'').slice(0,64)}
function showBoardDialog(){showModal(`<div class="role-modal"><h2>New Kanban</h2><div class="role-form"><label>Slug <span class="small">stable id, e.g. bhumi_claw</span></label><input id="boardNewSlug" placeholder="kanban_slug"><label>Name</label><input id="boardNewName" placeholder="Display name"><label>Workdir <span class="small">absolute project path</span></label><input id="boardNewWorkdir" placeholder="/Users/admin/code/project"><label>Description</label><textarea id="boardNewDesc" placeholder="optional"></textarea><div id="boardNewMsg" class="role-form-msg"></div></div><div class="modal-actions"><button class="layoutBtn" onclick="closeModal()">Cancel</button><button class="layoutBtn active" onclick="createBoard()">Create</button></div></div>`);setTimeout(()=>{const name=document.getElementById('boardNewName');const slug=document.getElementById('boardNewSlug');if(name&&slug){name.oninput=()=>{if(!slug.dataset.touched)slug.value=slugifyBoardName(name.value)};slug.oninput=()=>{slug.dataset.touched='1'}};document.getElementById('boardNewName')?.focus()},0)}
async function createBoard(){const msg=document.getElementById('boardNewMsg');const payload={slug:document.getElementById('boardNewSlug')?.value.trim(),name:document.getElementById('boardNewName')?.value.trim(),workdir:document.getElementById('boardNewWorkdir')?.value.trim(),description:document.getElementById('boardNewDesc')?.value.trim()};if(msg)msg.textContent='Creating...';try{const r=await fetch('/boards',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await r.json();if(!data.ok){if(msg)msg.textContent=data.error||'Create failed';return;}closeModal();sideMode='sessions';lastSideHtml='';saveState();await refresh();}catch(e){if(msg)msg.textContent=String(e)}}
function showModal(html){const el=document.getElementById('genericModal');const body=document.getElementById('genericModalBody');if(body)body.innerHTML=html;if(el)el.style.display='flex'}
function closeModal(){const el=document.getElementById('genericModal');if(el)el.style.display='none'}
async function createTask(){const msg=document.getElementById('taskMsg');const payload={board:document.getElementById('newTaskBoard')?.value,title:document.getElementById('newTaskTitle')?.value.trim(),mode:document.getElementById('newTaskMode')?.value,role:document.getElementById('newTaskRole')?.value,body:document.getElementById('newTaskBody')?.value.trim()};if(msg)msg.textContent='Creating...';try{const r=await fetch('/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await r.json();if(!data.ok){if(msg)msg.textContent=data.error||'Create failed';return;}hideTaskDialog();document.getElementById('newTaskTitle').value='';document.getElementById('newTaskBody').value='';sideMode='sessions';saveState();await refresh();}catch(e){if(msg)msg.textContent=String(e)}}
async function archiveBoard(b){if(!b)return;if(!confirm('Archive kanban '+b+'?'))return;try{const r=await fetch('/boards/archive',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({board:b})});const data=await r.json();if(!data.ok){alert(data.error||'Archive failed');return;}panes=panes.map(x=>{const rr=rolesById()[x];return rr&&rr.board===b?null:x});saveState();await refresh();}catch(e){alert(String(e))}}
function setupArchiveDrop(){const el=document.getElementById('archiveDrop');if(!el)return;el.ondragover=e=>{if(e.dataTransfer.types.includes('application/x-kanban-agency-board')){e.preventDefault();el.style.borderColor='#f59e0b';el.style.color='#fde68a'}};el.ondragleave=()=>{el.style.borderColor='#475569';el.style.color='#94a3b8'};el.ondrop=e=>{const b=e.dataTransfer.getData('application/x-kanban-agency-board');if(!b)return;e.preventDefault();el.style.borderColor='#475569';el.style.color='#94a3b8';archiveBoard(b)}}
const layouts={"1":[1],"2":[1,2],"3":[1,2,3],"2x2":[1,2,4,5],"3x2":[1,2,3,4,5,6],"left-split":[1,4,2,3],"main-side":[1,2,5]};
function visibleIds(){return layouts[layout]||[1,2,3]}
function paneIndex(id){return id-1}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function displayStatus(r){return r&&r.task_status==='done'?'idle':(r&&r.task_status)||'missing'}
function shortBoard(root){const b=root.board||''; const t=root.board_title||''; return (t||b).replace(/^Kanban Agency /,'').replace(/^kanban-agency-/,'').replace(/^kanban_agency_/,'').replace(/_/g,'-').slice(0,22)}
function shortRoot(root){let t=(root.title||'').replace(/^.*?\/\s*/,'').replace(/^\[(feature|bugfix)\]\s*/i,'').replace(/^需求分析师[：:]\s*梳理\s*/,'').replace(/需求$/,'').trim(); if(/历史会话/.test(t))return '历史会话'; if(/UI\s*风格/.test(t))return 'UI 风格调整'; if(/Docker|部署/.test(t))return 'Docker 部署'; if(/slug|离职|归属/.test(t))return 'slug 归属迁移'; if(/BFF.*demo|后端 demo/.test(t))return 'BFF demo'; if(t==='Independent tasks')return '独立任务'; return t.length>24?t.slice(0,23)+'…':t}
function statusIcon(s,p){if(p)return '<span class="st st-attention" title="needs attention">🔔</span>'; if(s==='blocked')return '<span class="st st-blocked" title="blocked">◆</span>'; if(s==='running')return '<span class="st st-running" title="running">●</span>'; if(s==='ready')return '<span class="st st-ready" title="ready">◇</span>'; if(s==='review')return '<span class="st st-review" title="review">◐</span>'; if(s==='todo')return '<span class="st st-todo" title="todo">○</span>'; if(s==='done')return '<span class="st st-done" title="done">✓</span>'; if(s==='missing')return '<span class="st st-missing" title="missing">?</span>'; return '<span class="st st-idle" title="idle">·</span>'}
function rootBadge(root){if(root.attention)return statusIcon(null,true); const roles=(root.roles||[]).filter(r=>r.task_id); if(roles.length&&roles.every(r=>r.task_status==='done'))return statusIcon('done',false); if(roles.some(r=>r.pending_approval))return statusIcon(null,true); if(roles.some(r=>r.task_status==='blocked'))return statusIcon('blocked',false); if(roles.some(r=>r.task_status==='running'))return statusIcon('running',false); if(roles.some(r=>r.task_status==='ready'))return statusIcon('ready',false); return statusIcon(root.status||'idle',false)}
function sym(s,p){return statusIcon(s,p)}
function cls(r){if(r.pending_approval)return 'blocked'; return r.task_status||'missing'}
function roleLabel(r){return `${esc(r.role||'session')}${paneRef(r.task_id)}`}
function allRoles(){return sessions.roots.flatMap(root=>root.roles.map(role=>Object.assign({},role,{root_title:root.title,root_id:root.root_id,board:root.board,board_title:root.board_title})))}
function nowLabel(r){return esc(shortRoot({title:r.root_title||r.display_title||r.title||r.role||'session'}))}
function roleCatalog(){return (sessions.available_roles||[]).filter(r=>r&&r.role&&r.board)}
function providerClass(p){return 'provider-'+String(p||'codex').toLowerCase()}
function providerLogo(p){p=String(p||'codex').toLowerCase();return p==='claude'?'✦':p==='hermes'?'✶':p==='human'?'◉':'C'}
function showRoleDetails(role){const rr=roleCatalog().find(x=>x.role===role);if(!rr)return;const rules=(rr.rules||[]).map(x=>`<li>${esc(x)}</li>`).join('')||'<li class="small">none</li>';const aliases=(rr.aliases||[]).map(x=>`<li>${esc(x)}</li>`).join('')||'<li class="small">none</li>';const cls=providerClass(rr.provider);showModal(`<div class="role-modal ${cls}"><h2><span class="role-logo">${providerLogo(rr.provider)}</span><span>${esc(rr.title||rr.role)}</span></h2><div class="small">${esc(rr.role)} · ${esc(rr.provider||'codex')}</div><p>${esc(rr.description||'No description')}</p><div class="role-detail-grid"><div class="role-detail-label">Provider</div><div>${esc(rr.provider||'codex')}</div><div class="role-detail-label">Rules</div><div><ul class="role-list">${rules}</ul></div><div class="role-detail-label">Aliases</div><div><ul class="role-list">${aliases}</ul></div></div><div class="modal-actions"><button class="layoutBtn" onclick="showRoleEditor('${esc(rr.role)}')">Edit</button><button class="layoutBtn" onclick="closeModal()">Close</button></div></div>`)}
async function showRoleEditor(role){const rr=roleCatalog().find(x=>x.role===role);if(!rr)return;roleRuleSources={};let ruleEditors='<div class="small">Loading rule sources...</div>';showModal(`<div class="role-modal ${providerClass(rr.provider)}"><h2><span class="role-logo">${providerLogo(rr.provider)}</span><span>Edit ${esc(rr.role)}</span></h2><div class="role-form"><label>Title</label><input id="roleEditTitle" value="${esc(rr.title||rr.role)}"><label>Description</label><textarea id="roleEditDesc">${esc(rr.description||'')}</textarea><label>Provider</label><select id="roleEditProvider"><option value="codex" ${rr.provider==='codex'?'selected':''}>codex</option><option value="claude" ${rr.provider==='claude'?'selected':''}>claude</option><option value="hermes" ${rr.provider==='hermes'?'selected':''}>hermes</option><option value="human" ${rr.provider==='human'?'selected':''}>human</option></select><label>Rules <span class="small">one absolute path per line</span></label><textarea id="roleEditRules">${esc((rr.rules||[]).join('\n'))}</textarea><div id="roleRuleSources">${ruleEditors}</div><label>Aliases <span class="small">one per line</span></label><textarea id="roleEditAliases">${esc((rr.aliases||[]).join('\n'))}</textarea><div id="roleEditMsg" class="role-form-msg"></div></div><div class="modal-actions"><button class="layoutBtn" onclick="showRoleDetails('${esc(rr.role)}')">Cancel</button><button class="layoutBtn active" onclick="saveRoleConfig('${esc(rr.role)}')">Save</button></div></div>`);await loadRoleRuleSources(role)}
async function loadRoleRuleSources(role){const box=document.getElementById('roleRuleSources');try{const resp=await fetch('/roles/'+encodeURIComponent(role)+'/rules',{cache:'no-store'});const data=await resp.json();if(!data.ok){if(box)box.innerHTML='<div class="small">'+esc(data.error||'Failed to load rules')+'</div>';return;}roleRuleSources={};let html='';for(const item of (data.rules||[])){roleRuleSources[item.path]=item.content||'';html+=`<div class="role-source"><div class="role-source-path">${esc(item.path)}${item.exists?'':' <span class="small">(new/missing)</span>'}</div>${item.editable?`<textarea data-rule-path="${esc(item.path)}">${esc(item.content||'')}</textarea>`:`<div class="small">${esc(item.error||'not editable')}</div>`}</div>`}if(!html)html='<div class="small">No rule files configured.</div>';if(box)box.innerHTML=html}catch(e){if(box)box.innerHTML='<div class="small">'+esc(String(e))+'</div>'}}
async function saveRoleConfig(role){const msg=document.getElementById('roleEditMsg');const lines=id=>(document.getElementById(id)?.value||'').split(/\n/).map(x=>x.trim()).filter(Boolean);const ruleContents={};document.querySelectorAll('textarea[data-rule-path]').forEach(t=>{ruleContents[t.dataset.rulePath]=t.value});const payload={role,title:document.getElementById('roleEditTitle')?.value.trim(),description:document.getElementById('roleEditDesc')?.value.trim(),provider:document.getElementById('roleEditProvider')?.value,rules:lines('roleEditRules'),aliases:lines('roleEditAliases'),rule_contents:ruleContents};if(msg)msg.textContent='Saving...';try{const r=await fetch('/roles/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await r.json();if(!data.ok){if(msg)msg.textContent=data.error||'Save failed';return;}sessions.available_roles=data.available_roles||sessions.available_roles;lastSideHtml='';renderSide();showRoleDetails(role);}catch(e){if(msg)msg.textContent=String(e)}}
function paneRef(task){const idx=panes.findIndex(x=>x===task);return idx>=0?`<span class="pane-ref">#${idx+1}</span>`:''}
function rootKey(root){return (root.board||'')+'/'+(root.root_id||root.title)}
function setLayout(l){layout=l;paneCount=visibleIds().length;saveState();document.getElementById('panes').className='panes layout-'+l;active=visibleIds().includes(active+1)?active:paneIndex(visibleIds()[0]);renderLayouts();renderPanes();}
function renderLayouts(){document.getElementById('layouts').innerHTML=Object.keys(layouts).map(l=>`<button class="layoutBtn ${l===layout?'active':''}" data-l="${l}">${l}</button>`).join('');document.querySelectorAll('#layouts .layoutBtn').forEach(b=>b.onclick=()=>setLayout(b.dataset.l));}
function pickDefaults(){const roles=allRoles().filter(r=>r.task_id); const ranked=[...roles.filter(r=>r.pending_approval),...roles.filter(r=>r.task_status==='running'&&!r.pending_approval),...roles.filter(r=>r.task_status==='ready'),...roles.filter(r=>r.task_status==='done')]; const ids=[...new Set(ranked.map(r=>r.task_id))]; visibleIds().forEach((id,pos)=>{const i=paneIndex(id); panes[i]=panes[i]||ids[pos]||null;});}
async function resumeTask(task){const i=panes.findIndex(x=>x===task);const paneIndexToUse=i>=0?i:active;try{const pane=document.querySelector(`.ph[data-pane="${paneIndexToUse}"]`)?.closest('.pane');const body=pane?.querySelector('.body');if(body)body.innerHTML='<div class="placeholder"><h3>Resuming TUI...</h3><p>'+esc(task)+'</p></div>';const resp=await fetch('/resume/'+encodeURIComponent(task),{cache:'no-store'});let data={};try{data=await resp.json()}catch(e){}if(data&&data.ok===false){if(body)body.innerHTML='<div class="placeholder"><h3>Resume failed</h3><pre class="summary">'+esc(JSON.stringify(data,null,2))+'</pre></div>';console.error('resume failed',data);return;}await refresh();replacePaneDom(paneIndexToUse);updatePaneHeaders();updatePaneFrames();setActive(paneIndexToUse);}catch(e){console.error(e)}}
async function openRole(role,b){try{const r=await fetch('/roles/'+encodeURIComponent(b||'')+'/'+encodeURIComponent(role)+'/open',{cache:'no-store'});const data=await r.json();if(data&&data.ok&&data.task_id){setPane(active,data.task_id);await refresh();}else{console.error('openRole failed',data)}}catch(e){console.error(e)}}
async function completeTask(task){if(!task)return;if(!confirm('Complete '+task+' in Kanban?'))return;try{const r=await fetch('/complete/'+encodeURIComponent(task),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});const data=await r.json();if(!data.ok){alert(data.error||'Complete failed');console.error('complete failed',data);return;}await refresh();const i=panes.findIndex(x=>x===task);if(i>=0)replacePaneDom(i);}catch(e){alert(String(e));console.error(e)}}
async function reopenTask(task){if(!task)return;if(!confirm('Reopen '+task+' as running?'))return;try{const r=await fetch('/reopen/'+encodeURIComponent(task),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'running'})});const data=await r.json();if(!data.ok){alert(data.error||'Reopen failed');console.error('reopen failed',data);return;}await refresh();const i=panes.findIndex(x=>x===task);if(i>=0)replacePaneDom(i);}catch(e){alert(String(e));console.error(e)}}
async function renameTask(task,current){if(!task)return;const title=prompt('Rename independent chat',current||'');if(title===null)return;const clean=title.trim();if(!clean)return;try{const r=await fetch('/tasks/'+encodeURIComponent(task)+'/title',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:clean})});const data=await r.json();if(!data.ok){alert(data.error||'Rename failed');console.error('rename failed',data);return;}lastSideHtml='';await refresh();const i=panes.findIndex(x=>x===task);if(i>=0)replacePaneDom(i);}catch(e){alert(String(e));console.error(e)}}
function clearDragging(){document.body.classList.remove('dragging')}
window.addEventListener('mouseup',clearDragging,true);window.addEventListener('pointerup',clearDragging,true);window.addEventListener('blur',clearDragging,true);document.addEventListener('visibilitychange',clearDragging,true);
function setSideMode(mode){sideMode=mode==='roles'?'roles':'sessions';saveState();lastSideHtml='';renderSide()}
function roleAttention(){return sessions.roots.filter(root=>String(root.root_id||'').startsWith('role:')).reduce((a,x)=>a+(x.attention||0),0)}
function kanbanAttention(){return sessions.roots.filter(root=>!String(root.root_id||'').startsWith('role:')).reduce((a,x)=>a+(x.attention||0),0)}
function syncSideTabs(){const s=document.getElementById('tabSessions');const r=document.getElementById('tabRoles');const ka=kanbanAttention();const ra=roleAttention();if(s){s.innerHTML='Kanbans'+(ka?` 🔔 ${ka}`:'');s.classList.toggle('active',sideMode!=='roles')}if(r){r.innerHTML='Roles'+(ra?` 🔔 ${ra}`:'');r.classList.toggle('active',sideMode==='roles')}}
function roleSessionRoots(){return sessions.roots.filter(root=>String(root.root_id||'').startsWith('role:'))}
function renderRoleSide(){let html='<div class="board-group roles-catalog"><div class="board-title">Roles <span class="small">definitions</span></div>'; const catalog=roleCatalog(); if(!catalog.length){html+='<div class="small">No roles available.</div>'} for(const rr of catalog){const pc=providerClass(rr.provider);html+=`<div class="role-card ${pc}" draggable="true" data-role="${esc(rr.role)}" data-board="${esc(rr.board)}" title="Click for details; drag to pane to open ${esc(rr.role)}"><div class="role-card-head"><div class="role-title"><span class="role-logo">${providerLogo(rr.provider)}</span><span class="role-name">${esc(rr.title||rr.role)}</span></div><span class="role-provider">${esc(rr.provider||'codex')}</span></div><div class="role-desc">${esc(rr.description||'No description')}</div><div class="role-action">click · drag</div></div>`} html+='</div>'; return html}
function pruneRecent(){recentTasks=recentTasks||{};const cutoff=Math.floor(Date.now()/1000)-3*24*60*60;for(const [id,ts] of Object.entries(recentTasks)){if(Number(ts||0)<cutoff)delete recentTasks[id]}}
function touchRecent(task){if(!task)return;recentTasks=recentTasks||{};recentTasks[task]=Math.floor(Date.now()/1000);pruneRecent();saveState()}
function seedRecentFromPanes(){recentTasks=recentTasks||{};let changed=false;const now=Math.floor(Date.now()/1000);for(const task of panes){if(task&&!recentTasks[task]){recentTasks[task]=now;changed=true}}pruneRecent();if(changed)saveState()}
function rootUnfinished(root){const roles=(root.roles||[]).filter(r=>r.task_id);if(!roles.length)return !!(root.status&&root.status!=='done'&&root.status!=='empty'&&root.status!=='archived');return roles.some(r=>!['done','archived'].includes(r.task_status||''))}
function renderRecentWorkset(){const sorted=[...(sessions.roots||[])].sort((a,b)=>Number(b.changed_at||0)-Number(a.changed_at||0));const unfinished=sorted.filter(rootUnfinished);const done=sorted.filter(r=>!rootUnfinished(r));const recent=[...unfinished,...done.slice(0,Math.max(0,5-unfinished.length))];if(!recent.length)return '';const label=unfinished.length>5?'unfinished roots':(unfinished.length?`unfinished + latest ${Math.max(0,5-unfinished.length)}`:'latest 5 roots');let html=`<div class="board-group recent-workset"><div class="board-title">Recent <span class="small">${label}</span></div>`;for(const root of recent){const key='recent/'+rootKey(root);const collapsed=collapsedRoots.has(key)||(root.collapsed&&!expandedRoots.has(key));html+=`<div class="root ${collapsed?'collapsed':'open'}"><div class="root-title ${collapsed?'closed':'open'}" title="${esc(root.title)}" data-root="${esc(key)}" data-default-collapsed="${root.collapsed?'1':'0'}"><span>${collapsed?'▸':'▾'}</span><span class="root-name">${esc(shortRoot(root))}</span><span class="root-state">${rootBadge(root)}</span></div>`;if(!collapsed){for(const r of (root.roles||[])){html+=`<button draggable="true" class="chip ${cls(r)}" title="${esc(r.title||'')}" data-task="${r.task_id||''}">${sym(r.task_status,r.pending_approval)} ${roleLabel(r)}</button>`}}html+='</div>'}html+='</div>';return html}
function renderSessionSide(){let html=renderRecentWorkset()+'<div class="board-group kanbans-group"><div class="board-title">Kanbans <span style="float:right;cursor:pointer" title="Create Kanban" onclick="event.stopPropagation();showBoardDialog()">+</span></div>'; let lastBoard=null; let currentBoardEmpty=true; for(const root of sessions.roots){const b=shortBoard(root);const boardSlug=root.board||'';const boardCollapsed=collapsedKanbans.has(boardSlug); if(b!==lastBoard){if(lastBoard!==null){if(currentBoardEmpty)html+='<div class="small" style="margin:4px 0 8px 12px">empty</div>';html+='</div>';} currentBoardEmpty=true; html+=`<div draggable="true" class="board-title" data-board-drag="${esc(boardSlug)}" data-kanban="${esc(boardSlug)}" title="drag to archive, click to collapse"><span>${boardCollapsed?'▸':'▾'}</span> ${esc(b)} <span class="root-state">${root.empty_board?'empty':''}</span> <span style="float:right;cursor:pointer" onclick="event.stopPropagation();showTaskDialog('${esc(boardSlug)}')">+</span></div>`; lastBoard=b;} if(boardCollapsed)continue; if(root.empty_board)continue; currentBoardEmpty=false; const key=rootKey(root); const collapsed=collapsedRoots.has(key)||(root.collapsed&&!expandedRoots.has(key));html+=`<div class="root ${collapsed?'collapsed':'open'}"><div class="root-title ${collapsed?'closed':'open'}" title="${esc(root.title)}" data-root="${esc(key)}" data-default-collapsed="${root.collapsed?'1':'0'}"><span>${collapsed?'▸':'▾'}</span><span class="root-name">${esc(shortRoot(root))}</span><span class="root-state">${rootBadge(root)}</span></div>`; if(!collapsed){for(const r of root.roles){html+=`<button draggable="true" class="chip ${cls(r)}" title="${esc(r.title||'')}" data-task="${r.task_id||''}">${sym(r.task_status,r.pending_approval)} ${roleLabel(r)}</button>`}} html+='</div>'} if(lastBoard!==null){if(currentBoardEmpty)html+='<div class="small" style="margin:4px 0 8px 12px">empty</div>';html+='</div>';} html+='</div>'; return html}
function renderSide(){syncSideTabs();let html=sideMode==='roles'?renderRoleSide():renderSessionSide(); if(html===lastSideHtml)return; lastSideHtml=html; document.getElementById('sessions').innerHTML=html; document.querySelectorAll('[data-board-drag]').forEach(el=>{el.onclick=e=>{if(e.target&&e.target.tagName==='SPAN')return;const b=el.dataset.kanban||el.dataset.boardDrag;if(collapsedKanbans.has(b))collapsedKanbans.delete(b);else collapsedKanbans.add(b);saveState();lastSideHtml='';renderSide()};el.ondragstart=e=>{e.dataTransfer.setData('application/x-kanban-agency-board',el.dataset.boardDrag);document.body.classList.add('dragging')};el.ondragend=clearDragging}); document.querySelectorAll('.root-title[data-root]').forEach(el=>{el.onclick=()=>{const k=el.dataset.root;const def=el.dataset.defaultCollapsed==='1';const collapsed=collapsedRoots.has(k)||(def&&!expandedRoots.has(k));if(collapsed){collapsedRoots.delete(k);expandedRoots.add(k)}else{expandedRoots.delete(k);collapsedRoots.add(k)}saveState();lastSideHtml='';renderSide();};}); document.querySelectorAll('.chip,.role-card').forEach(b=>{b.onclick=e=>{e.preventDefault();if(b.classList.contains('role-card')&&b.dataset.role)showRoleDetails(b.dataset.role);}; b.ondragstart=e=>{if(b.dataset.role){e.dataTransfer.setData('application/x-kanban-agency-role', JSON.stringify({role:b.dataset.role,board:b.dataset.board}));document.body.classList.add('dragging');return;} if(!b.dataset.task){e.preventDefault();return;} e.dataTransfer.setData('text/plain', b.dataset.task);document.body.classList.add('dragging');}; b.ondragend=clearDragging;});}
function desiredPaneSrc(r){if(!r)return ''; if(r.has_session&&!r.tmux_alive&&r.url)return `${r.url}?cockpit=1&t=${Date.now()}`; return `${(r.ttyd_url||r.url)}${(r.ttyd_url?'':'?cockpit=1&t='+Date.now())}`}
function paneBody(r){if(!r)return '<div class="placeholder">Choose a session from the left.</div>'; const hasLiveSession=!!(r.has_session&&r.live&&r.tmux_alive); if((r.task_status==='todo'||r.task_status==='ready')&&!r.parents_satisfied&&!hasLiveSession)return `<div class="placeholder"><h3>Waiting upstream</h3><p>${esc(r.title)}</p><p>${(r.parents||[]).map(p=>esc(p.title+' - '+p.status)).join('<br>')}</p></div>`; if(r.task_status==='missing')return '<div class="placeholder">Not created yet.</div>'; if(r.has_session&&!r.tmux_alive&&r.task_status==='done')return `<div class="placeholder"><h3>Stopped</h3><p>${esc(r.title)}</p><button class="layoutBtn" onclick="resumeTask('${esc(r.task_id)}')">Resume TUI</button><div class="summary">${esc((r.result||'').slice(0,2000))}</div></div>`; if(r.task_status==='done'&&r.has_session&&r.live&&r.tmux_alive)return `<iframe data-task="${r.task_id}" src="${desiredPaneSrc(r)}"></iframe>`; if(r.task_status==='done')return `<div class="placeholder"><h3>${r.has_session?'Stopped':'Idle'}</h3><p>${esc(r.title)}</p>${r.has_session?`<button class="layoutBtn" onclick="resumeTask('${esc(r.task_id)}')">Resume TUI</button>`:''}<div class="summary">${esc((r.result||'').slice(0,2000))}</div></div>`; return `<iframe data-task="${r.task_id}" src="${desiredPaneSrc(r)}"></iframe>`}
function rolesById(){return Object.fromEntries(allRoles().filter(r=>r.task_id).map(r=>[r.task_id,r]))}
function paneAction(r){if(!r||!r.task_id||r.task_status==='archived')return '';const rename=r.independent?`<button class="pane-action" title="Rename independent chat" onclick="event.stopPropagation();renameTask('${esc(r.task_id)}','${esc(r.display_title||r.title||'')}')">✎ Title</button>`:'';if(r.task_status==='done')return rename+`<button class="pane-action reopen" title="Reopen as running" onclick="event.stopPropagation();reopenTask('${esc(r.task_id)}')">↻ Running</button>`;return rename+`<button class="pane-action complete" title="Complete in Kanban" onclick="event.stopPropagation();completeTask('${esc(r.task_id)}')">✓ Complete</button>`}
function paneHeader(i,r){return `<span class="pane-id">#${i+1}</span>${r?`${sym(r.task_status,r.pending_approval)} ${esc(r.role)} - ${esc(displayStatus(r))} - ${esc(r.task_id)}${paneAction(r)}`:`empty`}`}
function paneHtml(i){const r=rolesById()[panes[i]]; return `<div class="pane ${i===active?'active':''}"><div class="ph" data-pane="${i}">${paneHeader(i,r)}</div><div class="body">${paneBody(r)}</div></div>`}
function setActive(i){active=i;if(panes[i])touchRecent(panes[i]);document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('active',Number(p.querySelector('.ph')?.dataset.pane)===active));}
function wirePane(pane,i){const h=pane.querySelector('.ph'); if(h)h.onclick=()=>setActive(i); const drop=e=>{e.preventDefault();clearDragging();pane.classList.remove('dropTarget');active=i;const roleRaw=e.dataTransfer.getData('application/x-kanban-agency-role'); if(roleRaw){try{const rr=JSON.parse(roleRaw);openRole(rr.role,rr.board);return;}catch(err){console.error(err)}} const task=e.dataTransfer.getData('text/plain'); if(task)setPane(i,task);}; const over=e=>{e.preventDefault();pane.classList.add('dropTarget')}; const leave=()=>pane.classList.remove('dropTarget'); pane.ondragover=over; pane.ondragleave=leave; pane.ondrop=drop; const b=pane.querySelector('.body'); if(b){b.ondragover=over;b.ondragleave=leave;b.ondrop=drop;}}
function replacePaneDom(i){const container=document.getElementById('panes');const pos=visibleIds().indexOf(i+1);const old=pos>=0?container.children[pos]:null; if(old){old.outerHTML=paneHtml(i); wirePane(container.children[pos],i);}}
function setPane(i,task){const from=panes.findIndex(x=>x===task);const targetOld=panes[i]; if(from>=0&&from!==i){panes[i]=task;panes[from]=targetOld||null;replacePaneDom(i);replacePaneDom(from);} else {panes[i]=task;replacePaneDom(i);} active=i;touchRecent(task);saveState();setActive(i);renderSide(); if(visibleIds().indexOf(i+1)<0)renderPanes(); updatePaneHeaders(); updatePaneFrames();}
function renderPanes(){let html=''; for(const id of visibleIds())html+=paneHtml(paneIndex(id)); document.getElementById('panes').innerHTML=html; document.querySelectorAll('.pane').forEach((pane,pos)=>wirePane(pane,paneIndex(visibleIds()[pos])));}
function updatePaneHeaders(){const roles=rolesById(); document.querySelectorAll('.pane').forEach((pane,pos)=>{const i=Number(pane.querySelector('.ph')?.dataset.pane);const r=roles[panes[i]]; const h=pane.querySelector('.ph'); if(h&&r){const next=paneHeader(i,r); if(h.dataset.last!==next){h.innerHTML=next;h.dataset.last=next;}}});}
function updatePaneFrames(){const roles=rolesById();document.querySelectorAll('iframe[data-task]').forEach(frame=>{const r=roles[frame.dataset.task];if(!r||frame.dataset.task!==r.task_id)return;const desired=desiredPaneSrc(r);if(desired&&frame.src!==desired)frame.src=desired;});}
async function refresh(){try{const r=await fetch('/sessions',{cache:'no-store'});sessions=await r.json();seedRecentFromPanes();const att=sessions.roots.reduce((a,x)=>a+(x.attention||0),0);const nextTitle=(att?'🔔 '+att+' · ':'')+'Session Cockpit';if(document.title!==nextTitle)document.title=nextTitle;const attention=document.getElementById('attention');const nextAtt=att?`🔔 ${att} need attention`:'';if(attention.textContent!==nextAtt)attention.textContent=nextAtt; if(visibleIds().every(id=>!panes[paneIndex(id)])){pickDefaults(); panesRenderedWithData=false;} if(!panesRenderedWithData){renderPanes(); panesRenderedWithData=true;} renderSide(); setupArchiveDrop(); updatePaneHeaders(); updatePaneFrames();}catch(e){console.error(e)}}
loadState();if(!layouts[layout])layout='3';paneCount=visibleIds().length;renderLayouts();document.getElementById('panes').className='panes layout-'+layout;setInterval(refresh,3000);refresh();
</script></body></html>"""
    return html.replace("__BOARD__", board).replace("__EMBED__", "hiddenHead" if embed else "")

def codex_web_gateway_start(port: int = CODEX_WEB_GATEWAY_PORT) -> dict[str, Any]:
    state = _read_json_file(CODEX_WEB_GATEWAY_STATE)
    if state.get("pid") and _pid_alive(state.get("pid")):
        return {"ok": True, "reused": True, "url": f"http://127.0.0.1:{state.get('port')}/", "state": state}
    CODEX_WEB_DIR.mkdir(parents=True, exist_ok=True)
    script = CODEX_WEB_DIR / "gateway.py"
    plugin_core = Path(__file__).resolve()
    gateway_code = """
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import html
import importlib.util, json, sys
CORE = {core!r}
PORT = {port!r}
spec = importlib.util.spec_from_file_location('kanban_agency_core_gateway', CORE)
core = importlib.util.module_from_spec(spec); sys.modules[spec.name] = core; spec.loader.exec_module(core)
class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): return
    def _no_cache_headers(self):
        self.send_header('cache-control','no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('pragma','no-cache')
        self.send_header('expires','0')
    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(status); self.send_header('content-type','application/json'); self._no_cache_headers(); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def _send_html(self, body, status=200):
        if isinstance(body, str): body = body.encode()
        self.send_response(status); self.send_header('content-type','text/html; charset=utf-8'); self._no_cache_headers(); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/boards':
            try:
                length = int(self.headers.get('content-length') or '0')
                raw = self.rfile.read(length).decode('utf-8') if length else '{{}}'
                payload = json.loads(raw or '{{}}')
            except Exception as exc:
                self._send_json({{'ok': False, 'error': 'invalid json: ' + str(exc)}}, status=400); return
            data = core.create_board_api(payload)
            self._send_json(data, status=200 if data.get('ok') else 400); return
        if path == '/roles/config':
            try:
                length = int(self.headers.get('content-length') or '0')
                raw = self.rfile.read(length).decode('utf-8') if length else '{{}}'
                payload = json.loads(raw or '{{}}')
            except Exception as exc:
                self._send_json({{'ok': False, 'error': 'invalid json: ' + str(exc)}}, status=400); return
            data = core.update_role_config_api(payload)
            self._send_json(data, status=200 if data.get('ok') else 400); return
        if path == '/tasks':
            try:
                length = int(self.headers.get('content-length') or '0')
                raw = self.rfile.read(length).decode('utf-8') if length else '{{}}'
                payload = json.loads(raw or '{{}}')
            except Exception as exc:
                self._send_json({{'ok': False, 'error': 'invalid json: ' + str(exc)}}, status=400); return
            board = str(payload.get('board') or '').strip()
            data = core.create_task_api(board, payload)
            self._send_json(data, status=200 if data.get('ok') else 400); return
        if path == '/boards/archive':
            try:
                length = int(self.headers.get('content-length') or '0')
                raw = self.rfile.read(length).decode('utf-8') if length else '{{}}'
                payload = json.loads(raw or '{{}}')
            except Exception as exc:
                self._send_json({{'ok': False, 'error': 'invalid json: ' + str(exc)}}, status=400); return
            data = core.archive_board_api(payload)
            self._send_json(data, status=200 if data.get('ok') else 400); return
        if path.startswith('/tasks/') and path.endswith('/title'):
            task_id = path.strip('/').split('/')[1].strip()
            try:
                length = int(self.headers.get('content-length') or '0')
                raw = self.rfile.read(length).decode('utf-8') if length else '{{}}'
                payload = json.loads(raw or '{{}}')
            except Exception as exc:
                self._send_json({{'ok': False, 'error': 'invalid json: ' + str(exc)}}, status=400); return
            data = core.update_task_title_api(task_id, payload)
            self._send_json(data, status=200 if data.get('ok') else 400); return
        if path.startswith('/reopen/'):
            task_id = path.strip('/').split('/', 1)[1].strip()
            try:
                length = int(self.headers.get('content-length') or '0')
                raw = self.rfile.read(length).decode('utf-8') if length else '{{}}'
                payload = json.loads(raw or '{{}}')
            except Exception as exc:
                self._send_json({{'ok': False, 'error': 'invalid json: ' + str(exc)}}, status=400); return
            data = core.reopen_task_api(task_id, payload)
            self._send_json(data, status=200 if data.get('ok') else 400); return
        if path.startswith('/complete/'):
            task_id = path.strip('/').split('/', 1)[1].strip()
            try:
                length = int(self.headers.get('content-length') or '0')
                raw = self.rfile.read(length).decode('utf-8') if length else '{{}}'
                payload = json.loads(raw or '{{}}')
            except Exception as exc:
                self._send_json({{'ok': False, 'error': 'invalid json: ' + str(exc)}}, status=400); return
            data = core.complete_task_api(task_id, payload)
            self._send_json(data, status=200 if data.get('ok') else 400); return
        self.send_response(404); self.end_headers()
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/':
            body = core._cockpit_html('__all__', embed=False)
            self._send_html(body); return
        if path == '/healthz':
            body = b'kanban-agency codex-web-gateway ok\\n'
            self.send_response(200); self.send_header('content-type','text/plain'); self._no_cache_headers(); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith('/tmux-scroll/'):
            task_id = path.strip('/').split('/', 1)[1].strip()
            try:
                qs = urlparse(self.path).query
                delta = int((qs.split('delta=',1)[1].split('&',1)[0]) if 'delta=' in qs else -800)
            except Exception:
                delta = -800
            body = json.dumps(core.tmux_scroll_task(task_id, delta=delta), ensure_ascii=False).encode()
            self.send_response(200); self.send_header('content-type','application/json'); self.send_header('Access-Control-Allow-Origin','*'); self.send_header('cache-control','no-store'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith('/view-text/'):
            task_id = path.strip('/').split('/', 1)[1].strip()
            body = core.task_view_text(task_id).encode()
            self.send_response(200); self.send_header('content-type','text/plain; charset=utf-8'); self.send_header('cache-control','no-store'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith('/view/'):
            task_id = path.strip('/').split('/', 1)[1].strip()
            body = core.task_view_html(task_id).encode()
            self.send_response(200); self.send_header('content-type','text/html; charset=utf-8'); self.send_header('cache-control','no-store'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith('/resume/'):
            task_id = path.strip('/').split('/', 1)[1].strip()
            board = core._find_board_for_task(task_id)
            if not board:
                body = json.dumps({{"ok": False, "error": "task not found"}}, ensure_ascii=False).encode()
            else:
                provider = None
                try:
                    conn = core.kb.connect(board=board)
                    try:
                        row = conn.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
                        if row:
                            provider = core._parse_role_body(row['body']).get('provider')
                    finally:
                        conn.close()
                except Exception:
                    provider = None
                if provider == 'claude':
                    data = core.run(board=board, task_id=task_id)
                else:
                    data = core.codex_web(board, task_id, reuse=False)
                body = json.dumps(data, ensure_ascii=False, indent=2).encode()
            self.send_response(200); self.send_header('content-type','application/json'); self.send_header('cache-control','no-store'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith('/maintenance/cleanup-completed-sessions'):
            qs = urlparse(self.path).query
            dry = 'dry_run=1' in qs or 'dry_run=true' in qs
            try:
                days = int((qs.split('days=',1)[1].split('&',1)[0]) if 'days=' in qs else 3)
            except Exception:
                days = 3
            body = json.dumps(core.cleanup_completed_sessions(max_age_days=days, dry_run=dry), ensure_ascii=False, indent=2).encode()
            self.send_response(200); self.send_header('content-type','application/json'); self._no_cache_headers(); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path == '/sessions':
            body = json.dumps(core.sessions_all(), ensure_ascii=False, indent=2).encode()
            self.send_response(200); self.send_header('content-type','application/json'); self.send_header('cache-control','no-store'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith('/roles/') and path.endswith('/rules'):
            parts = path.strip('/').split('/')
            role = parts[1].strip() if len(parts) >= 3 else ''
            body = json.dumps(core.role_rule_sources_api(role), ensure_ascii=False, indent=2).encode()
            self.send_response(200); self.send_header('content-type','application/json'); self._no_cache_headers(); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith('/roles/') and path.endswith('/open'):
            parts = path.strip('/').split('/')
            if len(parts) >= 4:
                board = parts[1].strip(); role = parts[2].strip()
                body = json.dumps(core.open_role_workspace(board, role), ensure_ascii=False, indent=2).encode()
            else:
                body = json.dumps({{"ok": False, "error": "invalid role open path"}}, ensure_ascii=False).encode()
            self.send_response(200); self.send_header('content-type','application/json'); self._no_cache_headers(); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith('/roles/'):
            parts = path.strip('/').split('/')
            board = parts[1].strip() if len(parts) > 1 else ''
            body = json.dumps({{"ok": True, "board": board, "available_roles": core._available_role_defs(board)}}, ensure_ascii=False, indent=2).encode()
            self.send_response(200); self.send_header('content-type','application/json'); self.send_header('cache-control','no-store'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path == '/boards':
            boards = []
            for b in core.kb.list_boards(include_archived=False):
                item = dict(b)
                item['default_workdir'] = item.get('default_workdir')
                boards.append(item)
            body = json.dumps({{'ok': True, 'boards': boards}}, ensure_ascii=False, indent=2).encode()
            self.send_response(200); self.send_header('content-type','application/json'); self.send_header('cache-control','no-store'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith('/sessions/'):
            board = path.strip('/').split('/', 1)[1].strip()
            body = json.dumps(core.sessions_status(board), ensure_ascii=False, indent=2).encode()
            self.send_response(200); self.send_header('content-type','application/json'); self.send_header('cache-control','no-store'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path == '/cockpit':
            embed = 'embed=1' in self.path
            body = core._cockpit_html('__all__', embed=embed)
            self._send_html(body); return
        if path.startswith('/status/'):
            task_id = path.strip('/').split('/', 1)[1].strip()
            board = core._find_board_for_task(task_id)
            if not board:
                bridge = core._load_bridge_state(task_id)
                board = bridge.get('board')
            body = json.dumps(core.session_alert_status(board, task_id), ensure_ascii=False, indent=2).encode()
            self.send_response(200); self.send_header('content-type','application/json'); self.send_header('cache-control','no-store'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith('/s/') or path.startswith('/codex/') or path.startswith('/claude/'):
            parts = path.strip('/').split('/', 1)
            prefix = parts[0]
            task_id = parts[1].strip() if len(parts) > 1 else ''
            board = None
            provider = None
            try:
                board = core._find_board_for_task(task_id)
                if board:
                    conn = core.kb.connect(board=board)
                    try:
                        row = conn.execute('SELECT body FROM tasks WHERE id=?', (task_id,)).fetchone()
                        if row:
                            provider = core._parse_role_body(row['body']).get('provider')
                    finally:
                        conn.close()
            except Exception:
                board = None
            if prefix == 'codex': provider = 'codex'
            if prefix == 'claude': provider = 'claude'
            if not board:
                bridge = core._load_bridge_state(task_id)
                board = bridge.get('board')
            if board:
                try:
                    conn = core.kb.connect(board=board)
                    try:
                        row = conn.execute('SELECT title,status FROM tasks WHERE id=?', (task_id,)).fetchone()
                        if row and not core._parents_satisfied(conn, task_id):
                            page = '<!doctype html><html><head><meta charset="utf-8"><title>Waiting '+html.escape(task_id)+'</title><style>body{{background:#0b0f14;color:#e5e7eb;font:14px system-ui;padding:24px}}</style></head><body><h2>Waiting for upstream role</h2><p>'+html.escape(row['title'] or task_id)+'</p><p>Status: '+html.escape(row['status'] or '')+'</p><p>This role is precreated but will not start until its upstream dependency is completed.</p></body></html>'
                            body = page.encode()
                            self.send_response(200); self.send_header('content-type','text/html; charset=utf-8'); self.send_header('cache-control','no-store'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
                    finally:
                        conn.close()
                except Exception:
                    pass
            if provider == 'claude':
                data = core.claude_web(board, task_id)
            else:
                if not board:
                    self.send_response(404); self.end_headers(); self.wfile.write(b'no board for task'); return
                if core.os.environ.get('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN') in ('1','true','yes'):
                    data = {{'ok': True, 'url': 'about:blank', 'spawn_disabled': True}}
                else:
                    data = core.codex_web(board, task_id)
            if not data.get('ok'):
                body = json.dumps(data, ensure_ascii=False, indent=2).encode()
                self.send_response(500); self.send_header('content-type','application/json'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
            target = data.get('url') or ''
            status_url = '/status/' + task_id
            provider_name = 'Claude' if provider == 'claude' else 'Codex'
            title = provider_name + ' ' + task_id
            page = '''<!doctype html><html><head><meta charset="utf-8"><title>{{title}}</title><style>
html,body,#frame{{margin:0;width:100%;height:100%;background:#0b0f14;overflow:hidden}}iframe{{border:0;width:100%;height:100%}}
#alert{{display:none;position:fixed;top:0;left:0;right:0;z-index:9999;background:#b45309;color:white;padding:8px 12px;font:14px system-ui,sans-serif;box-shadow:0 2px 10px #0008}}
#alert strong{{margin-right:8px}}
</style></head><body><div id="alert"><strong>🔔 Codex needs approval</strong><span id="reason"></span></div><iframe id="frame" data-src="{{target}}" allow="clipboard-read; clipboard-write"></iframe><script>
const normalTitle=document.title;let alerted=false;let loaded=false;let target='{{target}}';
function nudgeFrame(){{const f=document.getElementById('frame'); if(!f)return; const old=f.style.width; f.style.width='calc(100% - 1px)'; setTimeout(()=>{{f.style.width=old||'100%'; try{{f.contentWindow&&f.contentWindow.dispatchEvent(new Event('resize'));}}catch(e){{}}}},80);}}
function loadFrame(){{if(loaded||!target)return;loaded=true;const f=document.getElementById('frame');f.onload=()=>{{setTimeout(nudgeFrame,120);setTimeout(nudgeFrame,600);setTimeout(nudgeFrame,1500);}};f.src=target;}}
async function poll(){{try{{const r=await fetch('{{status_url}}',{{cache:'no-store'}});const s=await r.json();const el=document.getElementById('alert');
if(!loaded && (s.live || s.ttyd_url)) loadFrame();
if(s.pending_approval){{document.title='🔔 '+normalTitle;el.style.display='block';document.getElementById('reason').textContent=(s.pending&&s.pending.justification)||s.result||'Waiting for approval';if(!alerted){{alerted=true;try{{navigator.vibrate&&navigator.vibrate(200)}}catch(e){{}}}}}}
else{{document.title=normalTitle;el.style.display='none';alerted=false;}}}}catch(e){{}}}}
setInterval(poll,3000);setTimeout(loadFrame,2500);poll();
</script></body></html>'''
            page = page.replace('{{title}}', html.escape(title)).replace('{{target}}', html.escape(target, quote=True)).replace('{{status_url}}', status_url)
            body = page.encode()
            self.send_response(200); self.send_header('content-type','text/html; charset=utf-8'); self.send_header('cache-control','no-store'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
HTTPServer(('127.0.0.1', PORT), H).serve_forever()
""".format(core=str(plugin_core), port=int(port))
    script.write_text(gateway_code, encoding='utf-8')
    stdout_path = CODEX_WEB_DIR / 'gateway.stdout.log'
    stderr_path = CODEX_WEB_DIR / 'gateway.stderr.log'
    out = stdout_path.open('ab'); err = stderr_path.open('ab')
    try:
        proc = subprocess.Popen([sys.executable, str(script)], stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        out.close(); err.close()
    state = {"pid": proc.pid, "port": int(port), "url": f"http://127.0.0.1:{int(port)}/", "script": str(script), "stdout_log": str(stdout_path), "stderr_log": str(stderr_path), "started_at": int(time.time())}
    _write_json_file(CODEX_WEB_GATEWAY_STATE, state)
    return {"ok": True, "reused": False, "url": state["url"], "state": state}


def codex_web_gateway_stop() -> dict[str, Any]:
    state = _read_json_file(CODEX_WEB_GATEWAY_STATE)
    if not state:
        return {"ok": False, "error": "no gateway state"}
    pid = state.get("pid")
    if pid and _pid_alive(pid):
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except Exception:
            try: os.kill(int(pid), signal.SIGTERM)
            except Exception: pass
        deadline = time.time() + 3
        while time.time() < deadline and _pid_alive(pid): time.sleep(0.1)
        if _pid_alive(pid):
            try: os.killpg(int(pid), signal.SIGKILL)
            except Exception: pass
    state.update({"state": "stopped", "stopped_at": int(time.time())})
    _write_json_file(CODEX_WEB_GATEWAY_STATE, state)
    return {"ok": True, "state": state}

def scan(board: str, roles_path: Path = CONFIG_PATH) -> dict[str, Any]:
    errors: list[str] = []
    try:
        roles, role_warnings = load_roles(roles_path)
    except Exception as exc:
        return {"board": board, "roots": [], "errors": [str(exc)]}
    if not board:
        return {"board": board, "roots": [], "errors": ["--board is required"]}
    if not kb.board_exists(board):
        return {"board": board, "roots": [], "errors": [f"board not found: {board}"]}
    roots: list[dict[str, Any]] = []
    try:
        conn = kb.connect(board=board)
    except Exception as exc:
        return {"board": board, "roots": [], "errors": [f"cannot open board {board}: {exc}"]}
    try:
        for row in _task_rows(conn):
            t = kb.Task.from_row(row)
            warnings = list(role_warnings)
            if t.status in SKIP_STATUSES:
                continue
            if t.status not in ACTIVE_STATUSES:
                # Unknown/non-MVP statuses are not roots but should be visible as warning.
                continue
            if (t.title or "").startswith("[agency] "):
                continue
            body = t.body or ""
            if "@kanban-agency" not in body:
                continue
            workdir, wd_warnings = _resolve_workdir(body, board)
            warnings.extend(wd_warnings)
            matched, route_to, reasons = match_roles(t.title or "", body, roles)
            role = roles[route_to]
            warnings.extend(_rule_warnings(role, workdir))
            roots.append({
                "root_id": t.id,
                "title": t.title,
                "body": body,
                "status": t.status,
                "workdir": workdir,
                "agency_enabled": True,
                "matched_roles": matched,
                "route_to": route_to,
                "match_reasons": reasons,
                "warnings": warnings,
                "would_start_provider": role.provider,
                "would_create_title": f"[agency] {route_to}: {t.title}",
            })
    finally:
        conn.close()
    return {"board": board, "roots": roots, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kanban-agency")
    sub = parser.add_subparsers(dest="cmd", required=True)
    scan_p = sub.add_parser("scan")
    scan_p.add_argument("--board")
    scan_p.add_argument("--roles", default=str(CONFIG_PATH))
    start_p = sub.add_parser("start")
    start_p.add_argument("--board")
    start_p.add_argument("--roles", default=str(CONFIG_PATH))
    run_p = sub.add_parser("run")
    run_p.add_argument("--board")
    run_p.add_argument("--listen", default="ws://127.0.0.1:8795")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--task-id")
    cont_p = sub.add_parser("continue")
    cont_p.add_argument("--board")
    cont_p.add_argument("--listen", default="ws://127.0.0.1:8795")
    cont_p.add_argument("--dry-run", action="store_true")
    cont_p.add_argument("--task-id")
    sync_p = sub.add_parser("sync")
    sync_p.add_argument("--board")
    sync_p.add_argument("--task-id")
    adv_p = sub.add_parser("advance")
    adv_p.add_argument("--board")
    adv_p.add_argument("--root-id")
    adv_p.add_argument("--dry-run", action="store_true")
    wf_p = sub.add_parser("workflow-watch")
    wf_p.add_argument("--board")
    wf_p.add_argument("--interval", type=float, default=5.0)
    wf_p.add_argument("--once", action="store_true")
    wf_p.add_argument("--dry-run", action="store_true")
    mon_p = sub.add_parser("monitor")
    mon_p.add_argument("--board")
    mon_p.add_argument("--task-id")
    mon_p.add_argument("--dry-run", action="store_true")
    web_p = sub.add_parser("codex-web")
    web_p.add_argument("--board")
    web_p.add_argument("--task-id", required=True)
    web_p.add_argument("--port", type=int)
    web_p.add_argument("--no-reuse", action="store_true")
    web_stop_p = sub.add_parser("codex-web-stop")
    web_stop_p.add_argument("--board")
    web_stop_p.add_argument("--task-id", required=True)
    gw_p = sub.add_parser("codex-web-gateway")
    gw_p.add_argument("--port", type=int, default=CODEX_WEB_GATEWAY_PORT)
    gw_stop_p = sub.add_parser("codex-web-gateway-stop")
    args = parser.parse_args(argv)
    if args.cmd == "scan":
        data = scan(args.board, Path(args.roles))
        print(_json(data))
        return 1 if data.get("errors") else 0
    if args.cmd == "start":
        data = start(args.board, Path(args.roles))
        print(_json(data))
        return 1 if data.get("errors") else 0
    if args.cmd == "run":
        data = run(args.board, listen=args.listen, dry_run=args.dry_run, task_id=args.task_id)
        print(_json(data))
        return 1 if data.get("errors") else 0
    if args.cmd == "continue":
        data = continue_comments(args.board, listen=args.listen, dry_run=args.dry_run, task_id=args.task_id)
        print(_json(data))
        return 1 if data.get("errors") else 0
    if args.cmd == "sync":
        data = sync(args.board, task_id=args.task_id)
        print(_json(data))
        return 1 if data.get("errors") else 0
    if args.cmd == "advance":
        data = advance(args.board, root_id=args.root_id, dry_run=args.dry_run)
        print(_json(data))
        return 1 if data.get("errors") else 0
    if args.cmd == "workflow-watch":
        data = workflow_watch(args.board, interval=args.interval, once=args.once, dry_run=args.dry_run)
        print(_json(data))
        return 1 if data.get("errors") else 0
    if args.cmd == "monitor":
        data = monitor(args.board, task_id=args.task_id, dry_run=args.dry_run)
        print(_json(data))
        return 1 if data.get("errors") else 0
    if args.cmd == "codex-web":
        data = codex_web(args.board, args.task_id, port=args.port, reuse=not args.no_reuse)
        print(_json(data))
        return 0 if data.get("ok") else 1
    if args.cmd == "codex-web-stop":
        data = codex_web_stop(args.board, args.task_id)
        print(_json(data))
        return 0 if data.get("ok") else 1
    if args.cmd == "codex-web-gateway":
        data = codex_web_gateway_start(port=args.port)
        print(_json(data))
        return 0 if data.get("ok") else 1
    if args.cmd == "codex-web-gateway-stop":
        data = codex_web_gateway_stop()
        print(_json(data))
        return 0 if data.get("ok") else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
