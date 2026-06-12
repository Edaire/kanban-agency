import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_claude_resume_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_claude_interactive_does_not_reuse_ttyd_when_tmux_is_dead(monkeypatch, tmp_path):
    core = load_core()
    monkeypatch.setattr(core.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(core, '_read_claude_state', lambda task_id: {})
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'pid': 123, 'url': 'http://127.0.0.1:1/', 'tmux': 'dead-tmux'} if str(path).endswith('task-x.json') else {})
    monkeypatch.setattr(core, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(core, '_url_ok', lambda url: True)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: False)
    killed = []
    monkeypatch.setattr(core.os, 'kill', lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(core, '_ensure_claude_ops_settings', lambda: tmp_path / 'settings.json')
    calls = []
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    monkeypatch.setattr(core, '_free_port', lambda: 23456)
    monkeypatch.setattr(core, '_wait_for_tui_ready', lambda tmux: True)
    monkeypatch.setattr(core, '_ensure_prompt_submitted', lambda tmux, prompt: {'submitted': True})
    monkeypatch.setattr(core.subprocess, 'Popen', lambda *a, **k: SimpleNamespace(pid=456))
    monkeypatch.setattr(core, '_write_json_file', lambda *a, **k: None)
    class FakeConn:
        def close(self): pass
        def commit(self): pass
        def execute(self, *a, **k): return SimpleNamespace(fetchone=lambda: None)
    monkeypatch.setattr(core.kb, 'connect', lambda board=None: FakeConn())
    monkeypatch.setattr(core, '_set_status', lambda *a, **k: None)
    monkeypatch.setattr(core, 'ensure_claude_session_link', lambda *a, **k: None)
    monkeypatch.setattr(core.kb, 'add_comment', lambda *a, **k: None)

    task = SimpleNamespace(id='task-x', workspace_path=str(tmp_path), body='', title='T')
    out = core.claude_interactive_run_task('board-x', task, {'workdir': str(tmp_path)})
    assert out['ok'] is True
    assert out['reused'] is False
    assert killed
    assert any(cmd[:3] == ['tmux', 'new-session', '-d'] for cmd in calls)
