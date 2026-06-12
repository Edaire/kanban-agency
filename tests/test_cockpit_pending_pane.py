import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_pending_pane_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_permission_pending_does_not_show_kanban_complete_banner():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert "const completionPending=(pendingKind==='role_completed_waiting_complete'||pendingKind==='task_complete')" in html
    assert "r.pending_approval&&completionPending&&r.has_session" in html
    assert "r.pending_approval&&r.has_session" not in html
