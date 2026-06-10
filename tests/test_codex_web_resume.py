import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_codex_web_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeConn:
    def __init__(self, row):
        self.row = row
        self.comments = []
        self.closed = False

    def execute(self, sql, params=()):
        return self

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


def fake_task_row(task_id='t_resume'):
    return {
        'id': task_id,
        'title': 'resume task',
        'body': '@kanban-agency-role\nrole: researcher\nprovider: codex\nworkdir: /tmp\n',
        'status': 'done',
        'assignee': 'agency-researcher',
        'position': 0,
        'workspace_path': '/tmp',
        'result': 'done earlier',
        'created_at': 1,
        'updated_at': 1,
    }


def test_codex_web_resume_uses_clean_tmux_env_for_new_session_and_ttyd(monkeypatch, tmp_path):
    core = load_core()
    task_id = 't_resume'
    conn = FakeConn(fake_task_row(task_id))
    state_path = tmp_path / f'{task_id}.json'

    monkeypatch.delenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', raising=False)
    monkeypatch.setattr(core.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(core, 'CODEX_WEB_DIR', tmp_path)
    monkeypatch.setattr(core.kb.Task, 'from_row', lambda row: SimpleNamespace(id=row['id'], body=row['body'], workspace_path=row['workspace_path']))
    monkeypatch.setattr(core, '_codex_web_state_path', lambda tid: state_path)
    monkeypatch.setattr(core, '_read_json_file', lambda path: {
        'thread_id': 'thread-123',
        'cwd': str(tmp_path),
        'tmux_name': 'kanban-codex-t_resume',
    } if path == state_path else {})
    monkeypatch.setattr(core, '_load_bridge_state', lambda tid: {})
    monkeypatch.setattr(core, '_free_port', iter([41001, 41002]).__next__)
    monkeypatch.setattr(core, '_pid_alive', lambda pid: False)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: False)
    monkeypatch.setattr(core.kb, 'connect', lambda board: conn)
    monkeypatch.setattr(core.kb, 'add_comment', lambda conn, task_id, author, body: conn.comments.append(body))

    written = {}
    monkeypatch.setattr(core, '_write_json_file', lambda path, data: written.update({'path': path, 'data': data}))

    run_calls = []
    popen_calls = []

    def fake_run(cmd, **kwargs):
        run_calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0)

    class FakePopen:
        next_pid = 9000
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.kwargs = kwargs
            self.pid = FakePopen.next_pid
            FakePopen.next_pid += 1
            popen_calls.append((cmd, kwargs, self.pid))

    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    monkeypatch.setattr(core.subprocess, 'Popen', FakePopen)

    out = core.codex_web('board', task_id, reuse=False)

    assert out['ok'] is True
    assert run_calls, 'tmux new-session should be called when tmux session is absent'
    assert run_calls[0][0][:4] == ['tmux', 'new-session', '-d', '-s']
    assert 'TMUX' not in run_calls[0][1]['env']
    assert len(popen_calls) == 2
    assert all('TMUX' not in kwargs['env'] for _cmd, kwargs, _pid in popen_calls)
    assert popen_calls[0][0][:5] == ['/usr/bin/ttyd', '--interface', '127.0.0.1', '--port', '41001']
    assert ['tmux', 'attach-session', '-t', 'kanban-codex-t_resume'] == popen_calls[0][0][-4:]
    assert written['data']['url'] == 'http://127.0.0.1:41001/'
    assert written['data']['readonly_url'] == 'http://127.0.0.1:41002/'
    assert conn.closed is True


def test_codex_web_reports_reflow_required_when_no_thread_and_tmux_dead(monkeypatch, tmp_path):
    core = load_core()
    task_id = 't_no_thread'
    conn = FakeConn(fake_task_row(task_id))
    state_path = tmp_path / f'{task_id}.json'
    old_state = {
        'mode': 'native-tmux',
        'thread_id': None,
        'cwd': str(tmp_path),
        'tmux_name': 'kanban-codex-t_no_thread',
        'started_at': 1,
    }

    monkeypatch.delenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', raising=False)
    monkeypatch.setattr(core.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(core.kb.Task, 'from_row', lambda row: SimpleNamespace(id=row['id'], body=row['body'], workspace_path=row['workspace_path']))
    monkeypatch.setattr(core, '_codex_web_state_path', lambda tid: state_path)
    monkeypatch.setattr(core, '_read_json_file', lambda path: old_state if path == state_path else {})
    monkeypatch.setattr(core, '_load_bridge_state', lambda tid: {})
    monkeypatch.setattr(core, '_latest_codex_thread_for_cwd', lambda cwd, since: None)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: False)
    monkeypatch.setattr(core.kb, 'connect', lambda board: conn)

    def fail_native(*args, **kwargs):
        raise AssertionError('must not restart a new prompt when original thread was never captured')
    monkeypatch.setattr(core, 'codex_native_run_task', fail_native)

    out = core.codex_web('board', task_id, reuse=False)

    assert out['ok'] is False
    assert out['reflow_required'] is True
    assert out['reason'] == 'lost_native_session_without_thread'
    assert conn.closed is True


def test_codex_web_spawn_disable_prevents_resume_side_effects(monkeypatch, tmp_path):
    core = load_core()
    task_id = 't_disabled'
    conn = FakeConn(fake_task_row(task_id))
    state_path = tmp_path / f'{task_id}.json'

    monkeypatch.setenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', '1')
    monkeypatch.setattr(core.shutil, 'which', lambda name: f'/usr/bin/{name}')
    monkeypatch.setattr(core.kb.Task, 'from_row', lambda row: SimpleNamespace(id=row['id'], body=row['body'], workspace_path=row['workspace_path']))
    monkeypatch.setattr(core, '_codex_web_state_path', lambda tid: state_path)
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'thread_id': 'thread-123', 'cwd': str(tmp_path)} if path == state_path else {})
    monkeypatch.setattr(core, '_load_bridge_state', lambda tid: {})
    monkeypatch.setattr(core, '_pid_alive', lambda pid: False)
    monkeypatch.setattr(core.kb, 'connect', lambda board: conn)
    monkeypatch.setattr(core.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(AssertionError('should not spawn')))
    monkeypatch.setattr(core.subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(AssertionError('should not spawn')))

    out = core.codex_web('board', task_id, reuse=False)

    assert out['ok'] is False
    assert out['error'] == 'provider spawn disabled by KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN'
    assert out['thread_id'] == 'thread-123'
    assert conn.closed is True
