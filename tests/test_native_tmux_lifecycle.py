import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_native_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    hermes = home / '.hermes'
    home.mkdir()
    hermes.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(hermes))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_codex_native_run_starts_writable_and_readonly_ttyd(env, monkeypatch):
    core = load_core()
    board = 'native_start'
    workdir = str(env)
    core.kb.create_board(board, default_workdir=workdir)
    conn = core.kb.connect(board=board)
    try:
        root = core.kb.create_task(conn, title='Root', body='@kanban-agency', created_by='test', initial_status='running')
        body = core._make_role_card_body(root, 'developer', 'codex', workdir, 'Route work', 'instruction')
        task_id = core.kb.create_task(conn, title='[agency] developer: Route work', body=body, assignee='agency-developer', created_by='test', parents=[root], initial_status='running', workspace_path=workdir)
        row = conn.execute('select * from tasks where id=?', (task_id,)).fetchone()
        task = core.kb.Task.from_row(row)
    finally:
        conn.close()

    calls = []
    popens = []
    monkeypatch.setattr(core.shutil, 'which', lambda name: f'/bin/{name}')
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: False)
    monkeypatch.setattr(core, '_free_port', iter([41001, 41002]).__next__)
    monkeypatch.setattr(core, '_latest_codex_thread_for_cwd', lambda cwd, since: 'thread-native')

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    class FakePopen:
        _pid = 5000
        def __init__(self, cmd, **kwargs):
            type(self)._pid += 1
            self.pid = type(self)._pid
            self.cmd = cmd
            popens.append(cmd)

    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    monkeypatch.setattr(core.subprocess, 'Popen', FakePopen)
    monkeypatch.setattr(core.time, 'sleep', lambda *_: None)

    out = core.codex_native_run_task(board, task, core._parse_role_body(task.body))
    assert out['ok'] is True
    state = out['state']
    assert state['mode'] == 'native-tmux'
    assert state['url'] == 'http://127.0.0.1:41001/'
    assert state['readonly_url'] == 'http://127.0.0.1:41002/'
    assert state['pid'] != state['readonly_pid']
    assert len(popens) == 2
    writable, readonly = popens
    assert '--writable' in writable
    assert '--writable' not in readonly
    assert 'ttyd-wheel-index.html' in ' '.join(map(str, writable))
    assert 'ttyd-wheel-index.html' in ' '.join(map(str, readonly))
    assert any(cmd[:3] == ['tmux', 'new-session', '-d'] for cmd in calls)
    assert any(cmd == ['tmux', 'set-option', '-t', 'kanban-codex-' + task_id, '-g', 'mouse', 'off'] for cmd in calls)


def test_session_alert_prefers_native_web_thread_over_old_bridge(env, monkeypatch):
    core = load_core()
    board = 'thread_preference'
    workdir = str(env)
    core.kb.create_board(board, default_workdir=workdir)
    conn = core.kb.connect(board=board)
    try:
        root = core.kb.create_task(conn, title='Root', body='@kanban-agency', created_by='test', initial_status='running')
        body = core._make_role_card_body(root, 'developer', 'codex', workdir, 'Work', 'instruction')
        task_id = core.kb.create_task(conn, title='[agency] developer: Work', body=body, assignee='agency-developer', created_by='test', parents=[root], initial_status='running', workspace_path=workdir)
    finally:
        conn.close()

    core._write_json_file(core._codex_web_state_path(task_id), {
        'thread_id': 'native-thread',
        'tmux_name': 'kanban-codex-' + task_id,
        'url': 'http://127.0.0.1:1/',
        'readonly_url': 'http://127.0.0.1:2/',
    })
    monkeypatch.setattr(core, '_load_bridge_state', lambda task_id: {'thread_id': 'old-bridge-thread'})
    seen = {}
    def fake_live(task_id, thread_id=None):
        seen['thread_id'] = thread_id
        return {'live': True, 'thread_id': thread_id, 'tmux_alive': True, 'ttyd_alive': True, 'tmux_name': 'kanban'}
    monkeypatch.setattr(core, '_codex_native_session_live', fake_live)
    monkeypatch.setattr(core, '_codex_live_pending_approval', lambda thread_id: {'pending': False})

    st = core.session_alert_status(board, task_id)
    assert seen['thread_id'] == 'native-thread'
    assert st['thread_id'] == 'native-thread'
    assert st['readonly_ttyd_url'] == 'http://127.0.0.1:2/'


def test_run_codex_branch_does_not_call_appserver_runner(env, monkeypatch):
    core = load_core()
    board = 'native_run_only'
    workdir = str(env)
    core.kb.create_board(board, default_workdir=workdir)
    conn = core.kb.connect(board=board)
    try:
        root = core.kb.create_task(conn, title='Root', body='@kanban-agency', created_by='test', initial_status='running')
        body = core._make_role_card_body(root, 'developer', 'codex', workdir, 'Native only', 'instruction')
        task_id = core.kb.create_task(conn, title='[agency] developer: Native only', body=body, assignee='agency-developer', created_by='test', parents=[root], initial_status='running', workspace_path=workdir)
        with core.kb.write_txn(conn):
            conn.execute('update tasks set status=? where id=?', ('ready', task_id))
    finally:
        conn.close()

    called = {'native': 0, 'runner': 0}
    monkeypatch.setattr(core, '_load_codex_runner', lambda: (_ for _ in ()).throw(AssertionError('appserver runner should not be loaded')))
    def fake_native(board_arg, task, meta):
        called['native'] += 1
        return {'ok': True, 'state': {'mode': 'native-tmux'}, 'url': 'http://127.0.0.1/s'}
    monkeypatch.setattr(core, 'codex_native_run_task', fake_native)

    out = core.run(board, task_id=task_id)
    assert not out['errors']
    assert called['native'] == 1
    assert out['started'][0]['state']['mode'] == 'native-tmux'



def test_codex_live_ignores_stale_ttyd_without_tmux_or_codex(env, monkeypatch):
    core = load_core()
    task_id = 't_stale_ttyd'
    core._write_json_file(core._codex_web_state_path(task_id), {
        'provider': 'codex',
        'pid': 123,
        'url': 'http://127.0.0.1:9999/',
        'tmux_name': 'kanban-codex-stale',
        'thread_id': 'thread-stale',
    })
    monkeypatch.setattr(core, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: False)
    monkeypatch.setattr(core.subprocess, 'run', lambda *a, **k: type('CP', (), {'returncode': 1, 'stdout': ''})())

    live = core._codex_native_session_live(task_id, 'thread-stale', require_provider_process=True)

    assert live['ttyd_alive'] is True
    assert live['tmux_alive'] is False
    assert live['codex_alive'] is False
    assert live['live'] is False
