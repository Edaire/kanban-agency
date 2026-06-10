import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_role_workspace_resume_under_test', path)
    assert spec is not None and spec.loader is not None
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
    monkeypatch.delenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def make_board(core, board='role_resume', workdir='/tmp/work'):
    core.kb.create_board(board, name='Role Resume Board', default_workdir=workdir)
    return board


def test_independent_role_workspace_reuses_stopped_provider_until_explicit_exit(env, monkeypatch):
    core = load_core()
    board = make_board(core)
    init_calls = []
    monkeypatch.setattr(core, 'codex_native_init_role_session', lambda board, task, meta: init_calls.append(task.id) or {'ok': True, 'state': {'thread_id': 'thread', 'tmux_name': 'tmux'}})
    monkeypatch.setattr(core, '_role_workspace_provider_live', lambda state: False)
    monkeypatch.setattr(core, '_thread_has_explicit_exit', lambda thread_id: False)

    first = core.open_role_workspace(board, 'researcher')
    second = core.open_role_workspace(board, 'researcher')

    assert first['ok'] is True
    assert second['ok'] is True
    assert second['reused'] is True
    assert second['task_id'] == first['task_id']
    assert init_calls == [first['task_id']]

    state = core._read_role_workspace_state(core.INDEPENDENT_ROLE_BOARD, 'researcher')
    assert state['state'] == 'active'
    assert state['task_id'] == first['task_id']


def test_independent_role_workspace_explicit_exit_creates_new_session(env, monkeypatch):
    core = load_core()
    board = make_board(core)
    monkeypatch.setattr(core, 'codex_native_init_role_session', lambda board, task, meta: {'ok': True, 'state': {'thread_id': 'thread', 'tmux_name': 'tmux'}})

    first = core.open_role_workspace(board, 'researcher')
    monkeypatch.setattr(core, '_thread_has_explicit_exit', lambda thread_id: True)
    second = core.open_role_workspace(board, 'researcher')

    assert second['ok'] is True
    assert second['reused'] is False
    assert second['task_id'] != first['task_id']
