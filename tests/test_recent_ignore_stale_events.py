import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_recent_ignore_stale_under_test', path)
    assert spec is not None and spec.loader is not None
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
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_recent_changed_at_ignores_stale_events(env, monkeypatch):
    core = load_core()
    board = 'recent_ignore_stale'
    core.kb.create_board(board, name='Recent Ignore Stale')
    conn = core.kb.connect(board=board)
    try:
        root = core.kb.create_task(conn, title='Root task', body='@kanban-agency')
        role = core.kb.create_task(conn, title='[agency] assistant: Root task', body=f'@kanban-agency-role\nroot_id: {root}\nrole: assistant\nprovider: codex')
        conn.execute('INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)', (root, role))
        conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)", (role, 'stale', '{}', 2000000000))
        conn.commit()
        created = conn.execute('select created_at from tasks where id=?', (role,)).fetchone()['created_at']
    finally:
        conn.close()
    monkeypatch.setattr(core, 'session_alert_status', lambda board, task_id: {'live': False, 'pending_approval': False, 'pending': {'pending': False}})
    data = core.sessions_status(board)
    role_item = next(r for r in data['roots'][0]['roles'] if r.get('task_id') == role)
    assert created <= role_item['changed_at'] < 2000000000
    assert data['roots'][0]['changed_at'] < 2000000000
