"""kanban-agency Hermes plugin."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from .core import scan, start, run, continue_comments, sync, codex_web, codex_web_stop
except Exception:  # supports ad-hoc importlib loading in tests/tools
    import sys
    from pathlib import Path as _Path
    _plugin_dir = _Path(__file__).resolve().parent
    if str(_plugin_dir) not in sys.path:
        sys.path.insert(0, str(_plugin_dir))
    from core import scan, start, run, continue_comments, sync, codex_web, codex_web_stop


def kanban_agency_scan(args: dict[str, Any] | None = None, **_: Any) -> str:
    args = args or {}
    board = str(args.get("board") or "").strip()
    roles_path = Path(args.get("roles_path") or Path.home() / ".hermes" / "kanban-agency" / "roles.yaml")
    return json.dumps(scan(board, roles_path), ensure_ascii=False, indent=2)


def kanban_agency_start(args: dict[str, Any] | None = None, **_: Any) -> str:
    args = args or {}
    board = str(args.get("board") or "").strip()
    roles_path = Path(args.get("roles_path") or Path.home() / ".hermes" / "kanban-agency" / "roles.yaml")
    return json.dumps(start(board, roles_path), ensure_ascii=False, indent=2)


def kanban_agency_run(args: dict[str, Any] | None = None, **_: Any) -> str:
    args = args or {}
    board = str(args.get("board") or "").strip()
    listen = str(args.get("listen") or "ws://127.0.0.1:8795")
    dry_run = bool(args.get("dry_run", False))
    task_id = args.get("task_id")
    return json.dumps(run(board, listen=listen, dry_run=dry_run, task_id=task_id), ensure_ascii=False, indent=2)


def kanban_agency_continue(args: dict[str, Any] | None = None, **_: Any) -> str:
    args = args or {}
    board = str(args.get("board") or "").strip()
    listen = str(args.get("listen") or "ws://127.0.0.1:8795")
    dry_run = bool(args.get("dry_run", False))
    task_id = args.get("task_id")
    return json.dumps(continue_comments(board, listen=listen, dry_run=dry_run, task_id=task_id), ensure_ascii=False, indent=2)


def kanban_agency_sync(args: dict[str, Any] | None = None, **_: Any) -> str:
    args = args or {}
    board = str(args.get("board") or "").strip()
    task_id = args.get("task_id")
    return json.dumps(sync(board, task_id=task_id), ensure_ascii=False, indent=2)


def kanban_agency_codex_web(args: dict[str, Any] | None = None, **_: Any) -> str:
    args = args or {}
    board = str(args.get("board") or "").strip()
    task_id = str(args.get("task_id") or "").strip()
    port = args.get("port")
    reuse = bool(args.get("reuse", True))
    return json.dumps(codex_web(board, task_id, port=port, reuse=reuse), ensure_ascii=False, indent=2)


def kanban_agency_codex_web_stop(args: dict[str, Any] | None = None, **_: Any) -> str:
    args = args or {}
    board = str(args.get("board") or "").strip()
    task_id = str(args.get("task_id") or "").strip()
    return json.dumps(codex_web_stop(board, task_id), ensure_ascii=False, indent=2)
