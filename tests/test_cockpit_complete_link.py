import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_complete_link_under_test', path)
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
    monkeypatch.setenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', '1')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_complete_task_api_adds_last_agent_message_comment_and_completes(env, monkeypatch):
    core = load_core()
    board = 'complete_link'
    core.kb.create_board(board, name='Complete Link')
    conn = core.kb.connect(board=board)
    try:
        task_id = core.kb.create_task(
            conn,
            title='[agency] developer: sample',
            body='@kanban-agency-role\nrole: developer\nprovider: codex\nworkdir: /tmp\nroot_title: sample\n',
            assignee='agency-developer',
            created_by='test',
            initial_status='running',
        )
    finally:
        conn.close()

    monkeypatch.setattr(core, 'session_alert_status', lambda board, task_id: {
        'pending': {
            'pending': True,
            'kind': 'role_completed_waiting_complete',
            'last_agent_message': '最后一轮回答：测试通过，可以完成。',
        },
        'result': None,
    })

    out = core.complete_task_api(task_id, {})
    assert out['ok'] is True
    assert out['board'] == board
    assert out['comment'] == '最后一轮回答：测试通过，可以完成。'

    conn = core.kb.connect(board=board)
    try:
        row = conn.execute('select status,result from tasks where id=?', (task_id,)).fetchone()
        assert row['status'] == 'done'
        assert row['result'] == '最后一轮回答：测试通过，可以完成。'
        comment = conn.execute('select author,body from task_comments where task_id=? order by id desc limit 1', (task_id,)).fetchone()
        assert comment['author'] == 'cockpit'
        assert comment['body'] == '最后一轮回答：测试通过，可以完成。'
    finally:
        conn.close()


def test_cockpit_html_has_complete_button_for_pending_sessions(env):
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'async function completeTask(task)' in html
    assert "fetch('/complete/'" in html
    assert 'Complete in Kanban' in html
    assert 'Waiting for Kanban Complete' in html
