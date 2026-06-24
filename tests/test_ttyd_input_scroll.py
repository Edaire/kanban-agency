import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_ttyd_input_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_wheel_index_only_injects_wheel_scroll_handler_without_scrollbar_hacks():
    core = load_core()
    path = core.TTYD_WHEEL_INDEX
    assert path.exists()
    html = path.read_text(errors='replace')
    marker = '<script id="kanban-wheel-scroll-only">'
    assert marker in html
    injected = html.split(marker, 1)[1]
    assert "document.addEventListener('wheel'" in injected
    assert 'e.preventDefault()' in injected
    assert 'e.stopPropagation()' in injected
    assert '/tmux-scroll/' in injected
    assert 'vp.scrollTop += e.deltaY' not in injected
    assert '::-webkit-scrollbar' not in injected
    assert 'user-select' not in injected
    assert 'mousedown' not in injected
    assert 'mouseup' not in injected


def test_cockpit_uses_writable_ttyd_with_wheel_intercept_for_scroll_safety():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'r.ttyd_url||r.url' in html
    assert 'function updatePaneFrames()' in html
    assert 'desiredPaneSrc(r)' in html
    assert 'frame.src!==desired' in html
    assert "r.has_session&&!r.tmux_alive&&r.task_status==='done'" in html
    assert "r.has_session&&!r.tmux_alive&&r.url" in html
    assert 'function updatePaneFrames()' in html


def test_resume_tui_button_refreshes_same_pane_after_resume_without_auto_drag_resume():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'async function resumeTask' in html
    assert "panes.findIndex(x=>x===task)" in html
    assert "fetch('/resume/'+encodeURIComponent(task)" in html
    assert 'Resuming TUI' in html
    assert 'Resume failed' in html
    assert 'replacePaneDom(paneIndexToUse)' in html
    assert 'continueTaskInPane' not in html


def test_gateway_spawn_disable_guard_exists_for_tests():
    core = load_core()
    source = Path(core.__file__).read_text()
    assert 'KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN' in source
    assert 'provider spawn disabled by KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN' in source
    assert "'about:blank'" not in source

def test_writable_interaction_remains_in_s_route_not_cockpit_observer():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'r.ttyd_url||r.url' in html
    assert 'Open writable' not in html
