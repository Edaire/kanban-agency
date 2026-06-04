import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_editable_cockpit_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cockpit_default_pane_is_writable_ttyd_not_observer_view():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'r.ttyd_url||r.url' in html
    assert '/view/${r.task_id}' not in html
    assert 'Open writable' not in html


def test_wheel_index_stops_wheel_immediate_propagation_to_tui():
    core = load_core()
    path = core.TTYD_WHEEL_INDEX
    assert path.exists()
    injected = path.read_text(errors='replace').split('<script id="kanban-wheel-scroll-only">', 1)[1]
    assert "document.addEventListener('wheel'" in injected
    assert 'e.preventDefault()' in injected
    assert 'e.stopPropagation()' in injected
    assert 'e.stopImmediatePropagation()' in injected
    assert '/tmux-scroll/' in injected
    assert 'vp.scrollTop += e.deltaY' not in injected
