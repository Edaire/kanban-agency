import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_run_marks_running_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    hermes = home / '.hermes'
    home.mkdir(); hermes.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(hermes))
    return tmp_path


def make_role_task(core, board, provider):
    conn = core.kb.connect(board=board)
    body = f'''@kanban-agency-role
role: developer
provider: {provider}
workdir: /tmp
'''
    tid = core.kb.create_task(conn, title=f'[agency] developer: {provider} smoke', body=body, initial_status='running', board=board, workspace_kind='dir', workspace_path='/tmp')
    conn.execute("update tasks set status='ready' where id=?", (tid,))
    conn.commit()
    return tid


def status_of(core, board, task_id):
    conn = core.kb.connect(board=board)
    return conn.execute('select status from tasks where id=?', (task_id,)).fetchone()['status']


def test_codex_successful_start_marks_ready_task_running(env, monkeypatch):
    core = load_core()
    board = 'run_marks_codex'
    tid = make_role_task(core, board, 'codex')
    monkeypatch.setattr(core, 'codex_native_run_task', lambda board, task, meta: {'ok': True, 'state': {'tmux_name': 'tmux'}, 'url': 'http://127.0.0.1:1/'})

    result = core.run(board=board, task_id=tid)

    assert not result['errors']
    assert result['started'][0]['task_id'] == tid
    assert status_of(core, board, tid) == 'running'


def test_claude_interactive_start_marks_ready_task_running(env, monkeypatch):
    core = load_core()
    board = 'run_marks_claude'
    tid = make_role_task(core, board, 'claude')
    monkeypatch.setattr(core, 'claude_interactive_run_task', lambda board, task, meta: {'ok': True, 'state': {'reason': 'interactive_session', 'tmux': 'tmux'}})

    result = core.run(board=board, task_id=tid)

    assert not result['errors']
    assert result['started'][0]['task_id'] == tid
    assert status_of(core, board, tid) == 'running'
