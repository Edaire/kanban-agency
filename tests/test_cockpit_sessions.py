import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_cockpit_under_test', path)
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


def create_root_with_roles(core, board, title, workdir, statuses):
    kb = core.kb
    kb.create_board(board, name=board.replace('_', ' ').title(), default_workdir=workdir)
    conn = kb.connect(board=board)
    try:
        root = kb.create_task(
            conn,
            title=title,
            body=f'@kanban-agency\nworkdir: {workdir}\nworkflow: functional-development',
            created_by='test',
            workspace_kind='dir',
            workspace_path=workdir,
            initial_status='running',
        )
        with kb.write_txn(conn):
            conn.execute('update tasks set status=? where id=?', ('ready', root))
        made = {}
        parent = root
        for role, status in statuses:
            body = core._make_role_card_body(root, role, 'codex', workdir, title, 'instruction')
            task = kb.create_task(
                conn,
                title=f'[agency] {role}: {title}',
                body=body,
                assignee=core._agency_assignee(role),
                created_by='test',
                parents=[parent],
                initial_status='running',
                workspace_kind='dir',
                workspace_path=workdir,
            )
            with kb.write_txn(conn):
                core._set_status(conn, task, status, result='result')
            made[role] = task
            parent = task
        return root, made
    finally:
        conn.close()


def test_done_role_suppresses_stale_pending_bell(env, monkeypatch):
    core = load_core()
    board = 'bell_suppression'
    _, roles = create_root_with_roles(core, board, 'Finished feature', '/tmp/work', [('tester', 'done')])

    monkeypatch.setattr(core, '_codex_native_session_live', lambda task_id, thread_id=None: {
        'live': True, 'thread_id': 'thread-x', 'ttyd_alive': True, 'tmux_alive': True, 'tmux_name': 'tmux-x', 'url': 'http://127.0.0.1:1/'
    })
    monkeypatch.setattr(core, '_codex_live_pending_approval', lambda thread_id: {'pending': True, 'call_id': 'old-call'})
    monkeypatch.setattr(core, '_load_bridge_state', lambda task_id: {'thread_id': 'thread-x'})

    data = core.sessions_status(board)
    tester = next(r for root in data['roots'] for r in root['roles'] if r.get('task_id') == roles['tester'])
    assert tester['task_status'] == 'done'
    assert tester['pending_approval'] is False
    assert tester['pending'] is None
    assert data['roots'][0]['attention'] == 0


def test_sessions_status_orders_roots_newest_first_and_collapses_completed(env):
    core = load_core()
    board = 'root_order'
    old_root, _ = create_root_with_roles(core, board, 'Old done root', '/tmp/work', [('analyst', 'done'), ('architect', 'done')])
    new_root, _ = create_root_with_roles(core, board, 'New active root', '/tmp/work', [('analyst', 'running')])
    conn = core.kb.connect(board=board)
    try:
        with core.kb.write_txn(conn):
            conn.execute('update tasks set created_at=? where id=?', (100, old_root))
            conn.execute('update tasks set created_at=? where id=?', (200, new_root))
    finally:
        conn.close()

    data = core.sessions_status(board)
    assert data['roots'][0]['root_id'] == new_root
    assert data['roots'][1]['root_id'] == old_root
    assert data['roots'][1]['collapsed'] is True
    assert data['roots'][0]['collapsed'] is False


def test_sessions_all_includes_independent_role_tasks(env):
    core = load_core()
    board = 'independent_board'
    core.kb.create_board(board, name='Independent Board')
    conn = core.kb.connect(board=board)
    try:
        body = '@kanban-agency-role\nrole: assistant\nprovider: codex\nworkdir: /tmp/work\nroot_title: one-off\n\nrules:\n- /abs/assistant.md\n'
        task = core.kb.create_task(
            conn,
            title='[agency] assistant: one-off',
            body=body,
            assignee='agency-assistant',
            created_by='test',
            workspace_kind='dir',
            workspace_path='/tmp/work',
            initial_status='running',
        )
    finally:
        conn.close()

    data = core.sessions_all()
    independent_roots = [r for r in data['roots'] if r.get('root_id') == '__independent__']
    assert independent_roots
    assert any(role.get('task_id') == task for root in independent_roots for role in root['roles'])


def test_cockpit_html_has_drag_swap_resume_and_global_fetch(env):
    core = load_core()
    html = core._cockpit_html('__all__')
    assert "fetch(isAll?'/sessions'" in html
    assert 'const from=panes.findIndex' in html
    assert 'replacePaneDom(from)' in html
    assert 'resumeTask' in html
    assert 'body.dragging .body iframe{pointer-events:none}' in html
    assert 'pane::after' not in html


def test_session_alert_reports_tmux_lifecycle(env, monkeypatch):
    core = load_core()
    board = 'tmux_lifecycle'
    _, roles = create_root_with_roles(core, board, 'Lifecycle root', '/tmp/work', [('developer', 'running')])
    task_id = roles['developer']
    core._write_json_file(core._codex_web_state_path(task_id), {
        'task_id': task_id,
        'board': board,
        'provider': 'codex',
        'mode': 'native-tmux',
        'tmux_name': 'kanban-codex-test',
        'pid': 123,
        'url': 'http://127.0.0.1:123/',
        'thread_id': 'thread-life',
        'cwd': '/tmp/work',
    })
    monkeypatch.setattr(core, '_pid_alive', lambda pid: False)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: name == 'kanban-codex-test')
    monkeypatch.setattr(core, '_codex_live_pending_approval', lambda thread_id: {'pending': False})

    st = core.session_alert_status(board, task_id)
    assert st['live'] is True
    assert st['tmux_alive'] is True
    assert st['ttyd_alive'] is False
    assert st['tmux_name'] == 'kanban-codex-test'



def test_blocked_role_counts_as_attention_even_without_live_pending(env, monkeypatch):
    core = load_core()
    board = 'blocked_attention'
    _, roles = create_root_with_roles(core, board, 'Needs attention', '/tmp/work', [('tester', 'blocked')])
    monkeypatch.setattr(core, 'session_alert_status', lambda board, task_id: {
        'pending_approval': False,
        'pending': {'pending': False, 'reason': 'no_current_provider_bell'},
        'live': True,
    })

    data = core.sessions_status(board)
    tester = next(r for root in data['roots'] for r in root['roles'] if r.get('task_id') == roles['tester'])

    assert tester['pending_approval'] is True
    assert data['roots'][0]['attention'] == 1
