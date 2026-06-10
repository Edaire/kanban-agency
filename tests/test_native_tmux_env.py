import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_native_tmux_env_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeConn:
    def __init__(self):
        self.closed = False
        self.committed = False

    def close(self):
        self.closed = True

    def commit(self):
        self.committed = True


def test_codex_native_run_task_uses_clean_tmux_env_for_all_tmux_bound_processes(monkeypatch, tmp_path):
    core = load_core()
    task = SimpleNamespace(id='t_native_env', title='native env task', workspace_path=str(tmp_path), body='body')
    state_path = tmp_path / 'state.json'
    conn = FakeConn()

    monkeypatch.setenv('TMUX', 'polluted-tmux')
    monkeypatch.setattr(core.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(core, 'CODEX_WEB_DIR', tmp_path)
    monkeypatch.setattr(core, 'TTYD_WHEEL_INDEX', tmp_path / 'wheel.html')
    monkeypatch.setattr(core, '_codex_web_state_path', lambda task_id: state_path)
    monkeypatch.setattr(core, '_read_json_file', lambda path: {})
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: False)
    monkeypatch.setattr(core, '_free_port', iter([41011, 41012]).__next__)
    monkeypatch.setattr(core, '_latest_codex_thread_for_cwd', lambda cwd, since: 'thread-native')
    monkeypatch.setattr(core, '_ensure_prompt_submitted', lambda tmux_name, prompt_path: None)
    monkeypatch.setattr(core.kb, 'connect', lambda board: conn)
    monkeypatch.setattr(core, '_mark_running', lambda conn, task_id: None)
    monkeypatch.setattr(core, 'ensure_codex_session_link', lambda *a, **k: {'ok': True})
    monkeypatch.setattr(core.kb, 'add_comment', lambda *a, **k: None)

    writes = {}
    monkeypatch.setattr(core, '_write_json_file', lambda path, data: writes.update({'path': path, 'data': data}))

    run_calls = []
    popen_calls = []

    def fake_run(cmd, **kwargs):
        run_calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0)

    class FakePopen:
        next_pid = 9100
        def __init__(self, cmd, **kwargs):
            self.pid = FakePopen.next_pid
            FakePopen.next_pid += 1
            popen_calls.append((cmd, kwargs))

    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    monkeypatch.setattr(core.subprocess, 'Popen', FakePopen)

    out = core.codex_native_run_task('board', task, {'workdir': str(tmp_path)})

    assert out['ok'] is True
    assert run_calls
    assert popen_calls
    assert all('env' in kwargs and 'TMUX' not in kwargs['env'] for _cmd, kwargs in run_calls)
    assert all('env' in kwargs and 'TMUX' not in kwargs['env'] for _cmd, kwargs in popen_calls)
    assert writes['data']['tmux_name'] == 'kanban-codex-t_native_env'
    assert conn.closed is True
