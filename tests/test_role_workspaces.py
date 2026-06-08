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
    run_calls = []
    monkeypatch.setattr(core, 'run', lambda board, task_id=None, **kw: run_calls.append((board, task_id)) or {'board': board, 'started': [{'task_id': task_id}], 'errors': []})

    first = core.open_role_workspace(board, 'researcher')
    second = core.open_role_workspace(board, 'researcher')

    assert first['ok'] is True
    assert second['ok'] is True
    assert second['reused'] is True
    assert first['task_id'] == second['task_id']
    assert run_calls == [(board, first['task_id'])]

    conn = core.kb.connect(board=board)
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
    monkeypatch.setattr(core, 'run', lambda board, task_id=None, **kw: {'board': board, 'started': [{'task_id': task_id}], 'errors': []})

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
    assert all(r['board'] == board for r in data['available_roles'])


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
