"""Kanban management helpers for kanban-agency Cockpit.

This module contains CRUD-style operations for kanbans and kanban tasks. It is
kept separate from runtime session/tmux code so management UI changes do not
accidentally affect provider lifecycle behavior.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def create_kanban_api(*, kb, board_exists: Callable[[str], bool], payload: dict[str, Any]) -> dict[str, Any]:
    """Create a kanban with a required project workdir."""
    slug = str(payload.get("slug") or "").strip()
    name = str(payload.get("name") or "").strip() or None
    workdir = str(payload.get("workdir") or payload.get("default_workdir") or "").strip()
    if not slug:
        return {"ok": False, "error": "slug is required"}
    if not workdir:
        return {"ok": False, "error": "workdir is required"}
    if not workdir.startswith("/"):
        return {"ok": False, "error": "workdir must be an absolute path"}
    wd = Path(workdir).expanduser()
    if not wd.exists() or not wd.is_dir():
        return {"ok": False, "error": f"workdir does not exist or is not a directory: {workdir}"}
    try:
        meta = kb.create_board(
            slug,
            name=name,
            description=str(payload.get("description") or "").strip() or None,
            icon=str(payload.get("icon") or "").strip() or None,
            color=str(payload.get("color") or "").strip() or None,
            default_workdir=str(wd),
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "board": meta}


def archive_kanban_api(*, kb, protected_kanbans: set[str], payload: dict[str, Any]) -> dict[str, Any]:
    """Archive a kanban by setting board.json archived=true."""
    slug = str(payload.get("board") or payload.get("slug") or "").strip()
    if not slug:
        return {"ok": False, "error": "board is required"}
    if not kb.board_exists(slug):
        return {"ok": False, "error": f"board not found: {slug}"}
    if slug in protected_kanbans:
        return {"ok": False, "error": f"board cannot be archived: {slug}"}
    meta = kb.write_board_metadata(slug, archived=True)
    return {"ok": True, "board": meta}


def normalize_task_mode(raw: str | None) -> str | None:
    mode = str(raw or "workflow").strip().lower()
    if mode in {"four-role", "four_role", "flow", "workflow"}:
        return "workflow"
    if mode in {"independent", "single", "role"}:
        return "independent"
    return None


def make_workflow_root_body(*, title: str, body_text: str, workdir: str | None) -> str:
    body = "@kanban-agency\nworkflow: functional-development\n"
    if workdir:
        body += f"workdir: {workdir}\n"
    body += "\n" + (body_text or title) + "\n"
    return body


def make_independent_role_body(*, role: str, provider: str, title: str, instruction: str, workdir: str | None, rules: list[str]) -> str:
    rules_block = "\n".join(f"- {r}" for r in rules) if rules else "- (none)"
    return (
        "@kanban-agency-role\n"
        f"role: {role}\n"
        f"provider: {provider}\n"
        f"workdir: {workdir or ''}\n"
        f"root_title: {title}\n\n"
        "rules:\n"
        f"{rules_block}\n\n"
        "root_task_body:\n"
        "```text\n"
        "@kanban-agency\n"
        f"workdir: {workdir or ''}\n\n"
        f"{instruction}\n"
        "```\n\n"
        "@kanban-agency-independent\n"
        "session_policy: task_scoped\n"
    )


def create_kanban_task_api(
    *,
    kb,
    board: str,
    payload: dict[str, Any],
    resolve_workdir: Callable[[str, str], tuple[str | None, list[str]]],
    advance: Callable[..., dict[str, Any]],
    available_role_defs: Callable[[str], list[dict[str, Any]]],
    role_rules_for: Callable[[str], list[str]],
    agency_assignee: Callable[[str | None], str | None],
) -> dict[str, Any]:
    """Create either a four-role workflow root or a single independent role task."""
    if not board or not kb.board_exists(board):
        return {"ok": False, "error": f"board not found: {board}"}
    title = str(payload.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "title is required"}
    mode = normalize_task_mode(str(payload.get("mode") or payload.get("type") or "workflow"))
    if mode is None:
        return {"ok": False, "error": "mode must be workflow or independent"}
    body_text = str(payload.get("body") or payload.get("description") or "").strip()
    workdir, warnings = resolve_workdir(str(payload.get("workdir") or ""), board)
    conn = kb.connect(board=board)
    try:
        if mode == "workflow":
            task_id = kb.create_task(
                conn,
                title=title,
                body=make_workflow_root_body(title=title, body_text=body_text, workdir=workdir),
                assignee="kanban-agency",
                created_by="kanban-agency",
                workspace_kind="dir" if workdir else "scratch",
                workspace_path=workdir,
                initial_status="running",
            )
        else:
            task_id = None
    except Exception as exc:
        conn.close()
        return {"ok": False, "error": str(exc)}
    finally:
        if mode == "workflow":
            try:
                conn.close()
            except Exception:
                pass
    if mode == "workflow":
        # Important: close the root-task connection before advance(). advance()
        # opens its own connection and starts provider/session writes. Keeping
        # the first connection open while nested workflow creation starts has
        # triggered sqlite b-tree/index corruption on macOS with mixed readers.
        adv = advance(board, root_id=task_id, dry_run=False)
        return {"ok": True, "mode": "workflow", "task_id": task_id, "workdir": workdir, "warnings": warnings, "advance": adv}

    conn = kb.connect(board=board)
    try:
        role = str(payload.get("role") or "").strip().lower()
        if not role:
            return {"ok": False, "error": "role is required for independent task"}
        role_defs = {r.get("role"): r for r in available_role_defs(board)}
        provider = str(payload.get("provider") or (role_defs.get(role) or {}).get("provider") or "codex")
        instruction = body_text or title
        task_id = kb.create_task(
            conn,
            title=f"[agency] {role}: {title}",
            body=make_independent_role_body(
                role=role,
                provider=provider,
                title=title,
                instruction=instruction,
                workdir=workdir,
                rules=role_rules_for(role),
            ),
            assignee=agency_assignee(role),
            created_by="kanban-agency",
            workspace_kind="dir" if workdir else "scratch",
            workspace_path=workdir,
            initial_status="running",
        )
        return {"ok": True, "mode": "independent", "task_id": task_id, "role": role, "provider": provider, "workdir": workdir, "warnings": warnings}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()
