import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_observer_view_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_observer_view_route_exists_but_is_not_default_cockpit():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert '/view/${r.task_id}' not in html
    assert 'r.ttyd_url||r.url' in html


def test_task_view_html_is_plain_scrollable_observer(monkeypatch):
    core = load_core()
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'tmux_name': 'kanban-codex-t1', 'url': 'http://writable/'})
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    monkeypatch.setattr(core.subprocess, 'check_output', lambda cmd, text=True, stderr=None: 'line1\nline2\n')
    html = core.task_view_html('t1')
    assert '<pre' in html
    assert 'line1' in html
    assert 'line2' in html
    assert 'overflow:auto' in html
    assert 'Open writable' in html
    assert 'http://127.0.0.1:8766/s/t1' in html


def test_gateway_view_route_exists():
    core = load_core()
    gateway_html = core.codex_web_gateway_start.__doc__ or ''
    # Guard by source text because gateway is generated dynamically.
    source = Path(core.__file__).read_text()
    assert "path.startswith('/view/')" in source
    assert 'core.task_view_html' in source
