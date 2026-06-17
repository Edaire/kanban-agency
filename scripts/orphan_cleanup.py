#!/usr/bin/env python3
"""Conservative cleanup for orphan kanban-agency ttyd/tmux wrappers.

This handles historical leaks where ttyd/tmux/codex processes outlived their
kanban-agency state JSON, so normal task-based cleanup cannot find them.
Default is dry-run. Pass --apply to terminate candidates.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or HOME / ".hermes")
KA_HOME = HERMES_HOME / "kanban-agency"
BOARDS_DIR = HERMES_HOME / "kanban" / "boards"
ASSET_MARKERS = (
    "ttyd-wheel-index.html",
    "/kanban-agency/assets/",
    "/plugins/kanban-agency/assets/",
)
KEEP_TMUX = {
    "kanban-hermes-t_1b0740ff",  # current orchestrator session in active use
}
TASK_RE = re.compile(r"t_[A-Za-z0-9_]+")
TMUX_RE = re.compile(r"kanban-(?:codex|hermes|claude)-t_[A-Za-z0-9_]+")


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def ps_rows() -> list[dict]:
    out = run(["ps", "-axo", "pid=,ppid=,command=", "-ww"]).stdout
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "cmd": parts[2]})
        except ValueError:
            continue
    return rows


def tmux_sessions() -> set[str]:
    cp = run(["tmux", "list-sessions", "-F", "#S"])
    if cp.returncode != 0:
        return set()
    return {x.strip() for x in cp.stdout.splitlines() if x.strip()}


def load_state_files() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for base in [KA_HOME / "codex-web", KA_HOME / "hermes-web", KA_HOME / "claude-web"]:
        if not base.exists():
            continue
        for p in base.glob("*.json"):
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            task = data.get("task_id") or p.stem
            data["_path"] = str(p)
            out[str(task)] = data
    claude_run = KA_HOME / "claude-run"
    if claude_run.exists():
        for p in claude_run.glob("*/state.json"):
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            task = data.get("task_id") or p.parent.name
            data["_path"] = str(p)
            out.setdefault(str(task), data)
    return out


def task_status_map() -> dict[str, dict]:
    res: dict[str, dict] = {}
    if not BOARDS_DIR.exists():
        return res
    for board_dir in BOARDS_DIR.iterdir():
        db = board_dir / "kanban.db"
        bj = board_dir / "board.json"
        if not db.exists() or not bj.exists():
            continue
        try:
            meta = json.loads(bj.read_text())
            if meta.get("archived"):
                continue
        except Exception:
            pass
        try:
            con = sqlite3.connect(db)
            con.row_factory = sqlite3.Row
            for r in con.execute("select id,title,status,created_at,started_at,completed_at from tasks"):
                res[str(r["id"])] = {k: r[k] for k in r.keys()} | {"board": board_dir.name}
            con.close()
        except Exception:
            continue
    return res


def is_state_active(task_id: str, state: dict, tasks: dict, now: int, recent_days: int) -> bool:
    if (state.get("tmux_name") or state.get("tmux")) in KEEP_TMUX:
        return True
    task = tasks.get(task_id)
    status = (task or {}).get("status")
    if status in {"running", "ready", "blocked", "todo"}:
        return True
    if state.get("state") in {"running", "waiting_for_user", None, "blocked"} and status not in {"done", "archived"}:
        return True
    # Keep recently touched provider states even if task lookup failed/stale.
    try:
        p = Path(state.get("_path") or "")
        if p.exists() and p.stat().st_mtime >= now - recent_days * 86400:
            return True
    except Exception:
        pass
    return False


def terminate(pid: int, apply: bool) -> bool:
    if not apply:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            return True
        except Exception:
            break
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except Exception:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually terminate candidates")
    ap.add_argument("--recent-days", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    now = int(time.time())
    rows = ps_rows()
    states = load_state_files()
    tasks = task_status_map()
    tmux = tmux_sessions()
    state_tmux = {str(s.get("tmux_name") or s.get("tmux")) for s in states.values() if s.get("tmux_name") or s.get("tmux")}
    active_state_tmux = {
        str(s.get("tmux_name") or s.get("tmux"))
        for task, s in states.items()
        if (s.get("tmux_name") or s.get("tmux")) and is_state_active(task, s, tasks, now, args.recent_days)
    }
    state_tasks = set(states)

    ttyd_candidates = []
    for r in rows:
        cmd = r["cmd"]
        if "ttyd" not in cmd or not any(m in cmd for m in ASSET_MARKERS):
            continue
        tids = TASK_RE.findall(cmd)
        task_id = tids[-1] if tids else None
        tmux_names = TMUX_RE.findall(cmd)
        tmux_name = tmux_names[-1] if tmux_names else None
        reason = None
        if tmux_name in KEEP_TMUX:
            continue
        if task_id and task_id in states and is_state_active(task_id, states[task_id], tasks, now, args.recent_days):
            continue
        if task_id and task_id in states and not is_state_active(task_id, states[task_id], tasks, now, args.recent_days):
            reason = "stale_or_stopped_state"
        elif task_id and task_id not in states:
            reason = "no_state_for_task_id"
        elif not task_id:
            reason = "legacy_wrapper_no_task_id"
        if reason:
            ttyd_candidates.append({"pid": r["pid"], "ppid": r["ppid"], "task_id": task_id, "tmux_name": tmux_name, "reason": reason, "cmd": cmd[:260]})

    tmux_candidates = []
    for name in sorted(t for t in tmux if t.startswith("kanban-")):
        if name in KEEP_TMUX or name in active_state_tmux:
            continue
        if name not in state_tmux:
            reason = "no_state_for_tmux"
        else:
            reason = "state_not_active"
        tmux_candidates.append({"tmux_name": name, "reason": reason})

    killed_ttyd = []
    for c in ttyd_candidates:
        killed_ttyd.append({**c, "killed": terminate(c["pid"], args.apply)})
    killed_tmux = []
    for c in tmux_candidates:
        ok = False
        if args.apply:
            cp = run(["tmux", "kill-session", "-t", c["tmux_name"]])
            ok = cp.returncode == 0
        killed_tmux.append({**c, "killed": ok})

    summary = {
        "apply": args.apply,
        "counts_before": {
            "ttyd": sum(1 for r in rows if "ttyd" in r["cmd"]),
            "kanban_ttyd": sum(1 for r in rows if "ttyd" in r["cmd"] and any(m in r["cmd"] for m in ASSET_MARKERS)),
            "tmux_sessions": len(tmux),
            "kanban_tmux": sum(1 for t in tmux if t.startswith("kanban-")),
            "state_files": len(states),
            "active_state_tmux": len(active_state_tmux),
        },
        "ttyd_candidates": len(ttyd_candidates),
        "tmux_candidates": len(tmux_candidates),
        "killed_ttyd": killed_ttyd,
        "killed_tmux": killed_tmux,
        "keep_tmux": sorted(KEEP_TMUX | active_state_tmux),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({k: v for k, v in summary.items() if k not in {"killed_ttyd", "killed_tmux", "keep_tmux"}}, ensure_ascii=False, indent=2))
        print("sample ttyd candidates:")
        for c in ttyd_candidates[:20]:
            print(c)
        print("sample tmux candidates:")
        for c in tmux_candidates[:40]:
            print(c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
