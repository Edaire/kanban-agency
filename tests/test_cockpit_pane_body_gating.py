import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_pane_body_gating_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_running_live_session_not_blocked_by_upstream_and_no_completion_banner():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert "const hasLiveSession=!!(r.has_session&&r.live&&r.tmux_alive)" in html
    assert "(r.task_status==='todo'||r.task_status==='ready')&&!r.parents_satisfied&&!hasLiveSession" in html
    assert "Waiting for Kanban Complete" not in html
    assert "Complete in Kanban</button>" not in html
    assert "function paneAction(r)" in html
    assert "✓ Complete" in html
