import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_claude_cockpit_under_test', path)
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


def test_claude_session_alert_status_uses_claude_web_state(env, monkeypatch):
    core = load_core()
    board = 'claude_cockpit_board'
    conn = core.kb.connect(board=board)
    task_id = core.kb.create_task(conn, title='[agency] ops: smoke', body='@kanban-agency-role\nrole: ops\nprovider: claude\n', initial_status='blocked', board=board)
    core._write_json_file(core._claude_web_state_path(task_id), {
        'task_id': task_id,
        'provider': 'claude',
        'tmux': 'kanban-claude-' + task_id,
        'pid': 123,
        'url': 'http://127.0.0.1:12345/',
    })
    monkeypatch.setattr(core, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)

    st = core.session_alert_status(board, task_id)
    assert st['provider'] == 'claude'
    assert st['live'] is True
    assert st['tmux_alive'] is True
    assert st['ttyd_alive'] is True
    assert st['ttyd_url'] == 'http://127.0.0.1:12345/'

    data = core.sessions_status(board)
    role = next(r for root in data['roots'] for r in root['roles'] if r['task_id'] == task_id)
    assert role['live'] is True
    assert role['ttyd_url'] == 'http://127.0.0.1:12345/'
    assert role['has_session'] is True
