import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_recent_roots_under_test', path)
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


def test_recent_renders_changed_roots_like_kanbans_tree():
    core = load_core()
    html = core._cockpit_html('__all__')
    start = html.index('function renderRecentWorkset()')
    end = html.index('function renderSessionSide()', start)
    block = html[start:end]
    assert 'Recent <span class="small">latest 5 roots</span>' in block
    assert 'sessions.roots||[]' in block
    assert '.slice(0,5)' in block
    assert 'changed_at||0' in block
    assert 'cutoff' not in block
    assert 'shortRoot(root)' in block
    assert 'rootBadge(root)' in block
    assert 'roleLabel(r)' in block
    assert 'recentTasks' not in block
    assert 'nowLabel(r)' not in block


def test_sessions_exposes_changed_at_for_roots_and_roles(env, monkeypatch):
    core = load_core()
    board = 'recent_roots'
    core.kb.create_board(board, name='Recent Roots')
    conn = core.kb.connect(board=board)
    try:
        root = core.kb.create_task(conn, title='Root task', body='@kanban-agency')
        role = core.kb.create_task(conn, title='[agency] developer: Root task', body=f'@kanban-agency-role\nroot_id: {root}\nrole: developer\nprovider: codex')
        conn.execute('UPDATE tasks SET status=? WHERE id=?', ('running', root))
        conn.execute('UPDATE tasks SET status=? WHERE id=?', ('running', role))
        conn.execute('INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)', (root, role))
        conn.execute('INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?, ?, ?, ?)', (role, 'tester', 'changed', 9999999999))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(core, 'session_alert_status', lambda board, task_id: {'live': False, 'pending_approval': False, 'pending': {'pending': False}})
    data = core.sessions_status(board)
    root_item = data['roots'][0]
    role_item = next(r for r in root_item['roles'] if r.get('task_id') == role)
    assert role_item['changed_at'] == 9999999999
    assert root_item['changed_at'] == 9999999999
