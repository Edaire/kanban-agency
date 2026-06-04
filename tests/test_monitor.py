import importlib.util
import json
import os
from pathlib import Path

import pytest


def load_core():
    import sys
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('kanban_agency_core_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def core_env(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / '.hermes'))
    core = load_core()
    kb = core.kb
    board = 'monitor_smoke'
    kb.create_board(board)
    return core, kb, board, tmp_path


def make_role_task(core, kb, board, *, status='running', task_id_suffix=''):
    conn = kb.connect(board=board)
    try:
        root = kb.create_task(
            conn,
            title='root',
            body='@kanban-agency\nworkdir: /tmp\n',
            created_by='test',
            initial_status='running',
        )
        body = f'''@kanban-agency-role
root_id: {root}
role: tester
provider: codex
workdir: /tmp
root_title: root
'''
        task = kb.create_task(
            conn,
            title=f'[agency] tester: smoke {task_id_suffix}',
            body=body,
            assignee='agency-tester',
            created_by='test',
            initial_status='running',
        )
        with kb.write_txn(conn):
            core._set_status(conn, task, status, result=None)
        return root, task
    finally:
        conn.close()


def write_bridge_and_web(home, task, thread='thread-smoke', *, live=True):
    bridge = home / '.hermes' / 'codex-kanban-runs' / task / 'appserver-bridge' / 'bridge.json'
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_text(json.dumps({'thread_id': thread, 'state': 'blocked'}), encoding='utf-8')
    web = home / '.hermes' / 'kanban-agency' / 'codex-web' / f'{task}.json'
    web.parent.mkdir(parents=True, exist_ok=True)
    web.write_text(json.dumps({'pid': os.getpid() if live else 99999999, 'thread_id': thread, 'url': 'http://127.0.0.1:1/'}), encoding='utf-8')
    return thread


def write_session(home, thread, events):
    p = home / '.codex' / 'sessions' / '2026' / '06' / '01' / f'rollout-{thread}.jsonl'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
    return p


def approval_call(call_id='call_1'):
    return {
        'timestamp': '2026-06-01T00:00:00Z',
        'type': 'response_item',
        'payload': {
            'type': 'function_call',
            'name': 'exec_command',
            'call_id': call_id,
            'arguments': json.dumps({
                'cmd': 'bash smoke.sh',
                'workdir': '/tmp',
                'sandbox_permissions': 'require_escalated',
                'justification': 'needs approval',
            }),
        },
    }


def approval_output(call_id='call_1'):
    return {
        'timestamp': '2026-06-01T00:00:01Z',
        'type': 'response_item',
        'payload': {'type': 'function_call_output', 'call_id': call_id, 'output': 'ok'},
    }


def task_status(kb, board, task):
    conn = kb.connect(board=board)
    try:
        return dict(conn.execute('select status,result from tasks where id=?', (task,)).fetchone())
    finally:
        conn.close()


def test_monitor_marks_blocked_for_unanswered_live_approval(core_env):
    core, kb, board, home = core_env
    _, task = make_role_task(core, kb, board, status='running')
    thread = write_bridge_and_web(home, task)
    write_session(home, thread, [approval_call()])

    out = core.monitor(board, task_id=task)

    assert out['monitored'][0]['action'] == 'marked_blocked_native_approval'
    st = task_status(kb, board, task)
    assert st['status'] == 'blocked'
    assert 'needs approval' in st['result']


def test_monitor_marks_running_after_approval_output(core_env):
    core, kb, board, home = core_env
    _, task = make_role_task(core, kb, board, status='blocked')
    thread = write_bridge_and_web(home, task)
    write_session(home, thread, [approval_call(), approval_output()])

    out = core.monitor(board, task_id=task)

    assert out['monitored'][0]['action'] == 'marked_running_native_live'
    st = task_status(kb, board, task)
    assert st['status'] == 'running'


def test_monitor_skips_done_tasks_even_if_session_has_pending_approval(core_env):
    core, kb, board, home = core_env
    _, task = make_role_task(core, kb, board, status='done')
    thread = write_bridge_and_web(home, task)
    write_session(home, thread, [approval_call()])

    out = core.monitor(board)

    assert out['monitored'] == []
    assert task_status(kb, board, task)['status'] == 'done'


def test_monitor_only_updates_session_active_task_when_session_reused(core_env):
    core, kb, board, home = core_env
    _, old_task = make_role_task(core, kb, board, status='running', task_id_suffix='old')
    _, active_task = make_role_task(core, kb, board, status='running', task_id_suffix='active')
    thread = 'shared-thread'
    write_bridge_and_web(home, old_task, thread=thread)
    write_bridge_and_web(home, active_task, thread=thread)
    write_session(home, thread, [approval_call()])
    core._write_session_binding(thread, task_id=active_task, board=board, role='tester', root_id='root')

    out = core.monitor(board)
    actions = {x['task_id']: x['action'] for x in out['monitored']}

    assert actions[old_task] == 'skipped_session_bound_to_other_task'
    assert actions[active_task] == 'marked_blocked_native_approval'
    assert task_status(kb, board, old_task)['status'] == 'running'
    assert task_status(kb, board, active_task)['status'] == 'blocked'
