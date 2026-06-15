import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_recent_provider_activity_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    hermes = home / '.hermes'
    codex = home / '.codex' / 'sessions' / '2026' / '06' / '15'
    home.mkdir(); hermes.mkdir(); codex.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(hermes))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_recent_changed_at_includes_codex_session_file_mtime(env, monkeypatch):
    core = load_core()
    board = 'recent_provider'
    core.kb.create_board(board, name='Recent Provider')
    conn = core.kb.connect(board=board)
    try:
        root = core.kb.create_task(conn, title='Root task', body='@kanban-agency')
        role = core.kb.create_task(conn, title='[agency] architect: Root task', body=f'@kanban-agency-role\nroot_id: {root}\nrole: architect\nprovider: codex')
        conn.execute('UPDATE tasks SET status=? WHERE id=?', ('running', root))
        conn.execute('UPDATE tasks SET status=? WHERE id=?', ('running', role))
        conn.execute('INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)', (root, role))
        conn.commit()
    finally:
        conn.close()
    thread = '019eb0de-e7b0-7ca3-b3eb-ebc3ac845bdf'
    session = Path.home() / '.codex' / 'sessions' / '2026' / '06' / '15' / f'rollout-{thread}.jsonl'
    session.write_text('{"type":"event_msg","payload":{"type":"user_message","message":"hi"}}\n', encoding='utf-8')
    # Make provider activity newer than kanban created/comment/event timestamps.
    new_mtime = 2000000000
    import os
    os.utime(session, (new_mtime, new_mtime))
    core._write_json_file(core._codex_web_state_path(role), {'thread_id': thread})
    monkeypatch.setattr(core, 'session_alert_status', lambda board, task_id: {'live': True, 'pending_approval': False, 'pending': {'pending': False}, 'thread_id': thread})
    data = core.sessions_status(board)
    root_item = data['roots'][0]
    role_item = next(r for r in root_item['roles'] if r.get('task_id') == role)
    assert role_item['changed_at'] == new_mtime
    assert root_item['changed_at'] == new_mtime
