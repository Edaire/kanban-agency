import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_role_workspaces_under_test', path)
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


def make_board(core, board='role_ws', workdir='/tmp/work'):
    core.kb.create_board(board, name='Role Workspace Board', default_workdir=workdir)
    return board


def test_open_role_workspace_creates_independent_root_and_reuses_active(env, monkeypatch):
    core = load_core()
    board = make_board(core)
    init_calls = []
    monkeypatch.setattr(core, 'codex_native_init_role_session', lambda board, task, meta: init_calls.append((board, task.id)) or {'ok': True, 'state': {'thread_id': 'thread', 'tmux_name': 'tmux'}})

    first = core.open_role_workspace(board, 'researcher')
    second = core.open_role_workspace(board, 'researcher')

    assert first['ok'] is True
    assert second['ok'] is True
    assert second['reused'] is True
    assert first['task_id'] == second['task_id']
    assert init_calls == [(core.INDEPENDENT_ROLE_BOARD, first['task_id'])]

    conn = core.kb.connect(board=core.INDEPENDENT_ROLE_BOARD)
    try:
        root = conn.execute("select title from tasks where id=?", (first['root_id'],)).fetchone()
        role = conn.execute("select title, body, status from tasks where id=?", (first['task_id'],)).fetchone()
    finally:
        conn.close()
    assert root['title'] == 'Independent tasks'
    assert role['title'] == '[agency] researcher: independent chat'
    assert '@kanban-agency-independent' in role['body']
    assert 'role: researcher' in role['body']


def test_open_role_workspace_after_exit_creates_new_active_session(env, monkeypatch):
    core = load_core()
    board = make_board(core)
    monkeypatch.setattr(core, 'codex_native_init_role_session', lambda board, task, meta: {'ok': True, 'state': {'thread_id': 'thread', 'tmux_name': 'tmux'}})

    first = core.open_role_workspace(board, 'researcher')
    core.mark_role_workspace_exited(board, 'researcher', reason='user /exit')
    second = core.open_role_workspace(board, 'researcher')

    assert first['task_id'] != second['task_id']
    assert second['reused'] is False


def test_sessions_status_exposes_available_roles(env):
    core = load_core()
    board = make_board(core)

    data = core.sessions_status(board)

    roles = {r['role'] for r in data['available_roles']}
    assert {'researcher', 'analyst', 'architect', 'developer', 'tester', 'ops', 'assistant'} <= roles
    assert all(r['board'] == core.INDEPENDENT_ROLE_BOARD for r in data['available_roles'])


def test_cockpit_html_contains_role_drag_support(env):
    core = load_core()
    html = core._cockpit_html('role_ws')

    assert 'data-role' in html
    assert 'openRole' in html
    assert '/roles/' in html



def test_sessions_all_does_not_repeat_role_catalog_per_board(env):
    core = load_core()
    make_board(core, 'board_one')
    make_board(core, 'board_two')

    data = core.sessions_all()

    assert data['available_roles'] == []



def test_independent_role_sessions_are_collapsed_and_newest_first(env, monkeypatch):
    core = load_core()
    board = make_board(core)
    monkeypatch.setattr(core, 'codex_native_init_role_session', lambda board, task, meta: {'ok': True, 'state': {'thread_id': 'thread', 'tmux_name': 'tmux'}})

    first = core.open_role_workspace(board, 'researcher')
    core.mark_role_workspace_exited(board, 'researcher', reason='test')
    second = core.open_role_workspace(board, 'developer')
    conn = core.kb.connect(board=core.INDEPENDENT_ROLE_BOARD)
    try:
        with core.kb.write_txn(conn):
            conn.execute('update tasks set created_at=? where id=?', (100, first['task_id']))
            conn.execute('update tasks set created_at=? where id=?', (200, second['task_id']))
    finally:
        conn.close()

    data = core.sessions_status(core.INDEPENDENT_ROLE_BOARD)
    root = next(r for r in data['roots'] if r['title'] == 'Independent tasks')

    assert root['collapsed'] is True
    assert [r['task_id'] for r in root['roles'][:2]] == [second['task_id'], first['task_id']]



def test_open_role_workspace_uses_independent_board_and_does_not_run_prompt(env, monkeypatch):
    core = load_core()
    source_board = make_board(core, 'source_feature_board')
    calls = []
    monkeypatch.setattr(core, 'run', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('open role must not call run')))
    monkeypatch.setattr(core, 'codex_native_init_role_session', lambda board, task, meta: calls.append((board, task.id, meta.get('role'))) or {'ok': True, 'url': f'/s/{task.id}', 'state': {'thread_id': 'thread-1', 'tmux_name': 'tmux-1'}})

    opened = core.open_role_workspace(source_board, 'researcher')

    assert opened['ok'] is True
    assert opened['board'] == core.INDEPENDENT_ROLE_BOARD
    assert core.kb.board_exists(core.INDEPENDENT_ROLE_BOARD)
    assert calls == [(core.INDEPENDENT_ROLE_BOARD, opened['task_id'], 'researcher')]
    assert not core.kb.board_exists('source_feature_board') or opened['board'] != source_board


def test_available_roles_point_to_independent_board(env):
    core = load_core()
    make_board(core, 'source_feature_board')

    roles = core._available_role_defs('source_feature_board')

    assert roles
    assert {r['board'] for r in roles} == {core.INDEPENDENT_ROLE_BOARD}
