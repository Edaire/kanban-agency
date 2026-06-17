import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_tmux_scroll_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_ttyd_wheel_index_uses_tmux_scroll_endpoint_not_xterm_viewport():
    core = load_core()
    html = (core.TTYD_WHEEL_INDEX).read_text(errors='replace')
    injected = html.split('<script id="kanban-wheel-scroll-only">', 1)[1]
    assert '/tmux-scroll/' in injected
    assert 'mode:\'no-cors\'' in injected or 'mode:"no-cors"' in injected
    assert 'vp.scrollTop += e.deltaY' not in injected
    assert 'stopImmediatePropagation' in injected


def test_tmux_scroll_task_sends_copy_mode_scroll(monkeypatch):
    core = load_core()
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'tmux_name': 'kanban-codex-t1'})
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(core.subprocess, 'run', fake_run)

    out = core.tmux_scroll_task('t1', delta=-800)
    assert out['ok'] is True
    assert ['tmux', 'copy-mode', '-e', '-t', 'kanban-codex-t1'] in calls
    assert any(cmd[:4] == ['tmux', 'send-keys', '-t', 'kanban-codex-t1'] and 'scroll-up' in cmd for cmd in calls)

    calls.clear()
    monkeypatch.setattr(core.subprocess, 'check_output', lambda *args, **kwargs: '1')
    out = core.tmux_scroll_task('t1', delta=800)
    assert out['ok'] is True
    assert any('scroll-down' in cmd for cmd in calls)


def test_tmux_scroll_down_at_live_bottom_is_noop(monkeypatch):
    core = load_core()
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'tmux_name': 'kanban-codex-t1'})
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    calls = []
    monkeypatch.setattr(core.subprocess, 'run', lambda cmd, **kwargs: calls.append(cmd) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(core.subprocess, 'check_output', lambda *args, **kwargs: '0')
    out = core.tmux_scroll_task('t1', delta=120)
    assert out['ok'] is True
    assert out['steps'] == 0
    assert out['at_bottom'] is True
    assert calls == []


def test_gateway_has_tmux_scroll_route():
    core = load_core()
    source = Path(core.__file__).read_text()
    assert "path.startswith('/tmux-scroll/')" in source
    assert 'core.tmux_scroll_task' in source
