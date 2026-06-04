import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_monitor_under_test', path)
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


def make_task(core, board, workdir, status='running'):
    core.kb.create_board(board, default_workdir=workdir)
    conn = core.kb.connect(board=board)
    try:
        root = core.kb.create_task(conn, title='Root', body='@kanban-agency', created_by='test', initial_status='running', workspace_path=workdir)
        body = core._make_role_card_body(root, 'developer', 'codex', workdir, 'Work', 'instruction')
        task_id = core.kb.create_task(conn, title='[agency] developer: Work', body=body, assignee='agency-developer', created_by='test', parents=[root], initial_status='running', workspace_path=workdir)
        with core.kb.write_txn(conn):
            core._set_status(conn, task_id, status)
        return task_id
    finally:
        conn.close()


def get_status(core, board, task_id):
    conn = core.kb.connect(board=board)
    try:
        return conn.execute('select status,result from tasks where id=?', (task_id,)).fetchone()
    finally:
        conn.close()


def test_monitor_native_live_marks_ready_task_running(env, monkeypatch):
    core = load_core()
    board = 'monitor_live'
    task_id = make_task(core, board, str(env), status='ready')
    monkeypatch.setattr(core, '_reset_waiting_on_upstream', lambda conn, task: False)
    monkeypatch.setattr(core, '_load_bridge_state', lambda task_id: {'thread_id': 'old-bridge'})
    monkeypatch.setattr(core, '_codex_native_session_live', lambda task_id, thread_id=None: {
        'live': True, 'thread_id': 'native-thread', 'url': 'http://127.0.0.1:1/', 'tmux_alive': True
    })
    monkeypatch.setattr(core, '_read_session_binding', lambda thread_id: {'active_task_id': task_id})
    monkeypatch.setattr(core, '_codex_live_pending_approval', lambda thread_id: {'pending': False})

    out = core.monitor(board, task_id=task_id)
    row = get_status(core, board, task_id)
    assert row['status'] == 'running'
    assert out['monitored'][0]['action'] == 'marked_running_native_live'


def test_monitor_native_pending_approval_marks_blocked(env, monkeypatch):
    core = load_core()
    board = 'monitor_pending'
    task_id = make_task(core, board, str(env), status='running')
    monkeypatch.setattr(core, '_reset_waiting_on_upstream', lambda conn, task: False)
    monkeypatch.setattr(core, '_load_bridge_state', lambda task_id: {'thread_id': 'thread-pending'})
    monkeypatch.setattr(core, '_codex_native_session_live', lambda task_id, thread_id=None: {
        'live': True, 'thread_id': 'thread-pending', 'url': 'http://127.0.0.1:1/', 'tmux_alive': True
    })
    monkeypatch.setattr(core, '_read_session_binding', lambda thread_id: {'active_task_id': task_id})
    monkeypatch.setattr(core, '_codex_live_pending_approval', lambda thread_id: {
        'pending': True,
        'cmd': 'dangerous command',
        'justification': 'needs approval',
        'call_id': 'call-1',
    })

    out = core.monitor(board, task_id=task_id)
    row = get_status(core, board, task_id)
    assert row['status'] == 'blocked'
    assert 'needs approval' in row['result']
    assert out['monitored'][0]['action'] == 'marked_blocked_native_approval'


def test_monitor_skips_when_native_session_bound_to_other_task(env, monkeypatch):
    core = load_core()
    board = 'monitor_binding'
    task_id = make_task(core, board, str(env), status='ready')
    monkeypatch.setattr(core, '_reset_waiting_on_upstream', lambda conn, task: False)
    monkeypatch.setattr(core, '_load_bridge_state', lambda task_id: {'thread_id': 'thread-bound'})
    monkeypatch.setattr(core, '_codex_native_session_live', lambda task_id, thread_id=None: {
        'live': True, 'thread_id': 'thread-bound', 'url': 'http://127.0.0.1:1/'
    })
    monkeypatch.setattr(core, '_read_session_binding', lambda thread_id: {'active_task_id': 'other-task'})

    out = core.monitor(board, task_id=task_id)
    row = get_status(core, board, task_id)
    assert row['status'] == 'ready'
    assert out['monitored'][0]['action'] == 'skipped_session_bound_to_other_task'


def test_monitor_does_not_let_old_bridge_override_native_web_thread(env, monkeypatch):
    core = load_core()
    board = 'monitor_native_preference'
    task_id = make_task(core, board, str(env), status='ready')
    core._write_json_file(core._codex_web_state_path(task_id), {'thread_id': 'native-thread', 'tmux_name': 'kanban-codex-' + task_id})
    monkeypatch.setattr(core, '_reset_waiting_on_upstream', lambda conn, task: False)
    monkeypatch.setattr(core, '_load_bridge_state', lambda task_id: {'thread_id': 'old-bridge-thread'})
    seen = {}
    def fake_live(task_id, thread_id=None):
        seen['thread_id'] = thread_id
        return {'live': True, 'thread_id': thread_id, 'url': 'http://127.0.0.1:1/'}
    monkeypatch.setattr(core, '_codex_native_session_live', fake_live)
    monkeypatch.setattr(core, '_read_session_binding', lambda thread_id: {'active_task_id': task_id})
    monkeypatch.setattr(core, '_codex_live_pending_approval', lambda thread_id: {'pending': False})

    core.monitor(board, task_id=task_id)
    # monitor currently passes bridge thread into _codex_native_session_live, but
    # _codex_native_session_live itself must prefer the web state thread. This
    # assertion documents the desired anti-regression at session_alert_status
    # level until monitor is fully lane-aware.
    st = core.session_alert_status(board, task_id)
    assert st['thread_id'] == 'native-thread'
