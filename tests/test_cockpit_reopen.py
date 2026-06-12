import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_reopen_under_test', path)
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


def test_reopen_task_api_moves_done_to_running(env):
    core = load_core()
    board = 'reopen_board'
    core.kb.create_board(board, name='Reopen Board')
    conn = core.kb.connect(board=board)
    try:
        task_id = core.kb.create_task(conn, title='Done task', body='body')
        conn.execute('UPDATE tasks SET status=?, completed_at=? WHERE id=?', ('done', 123, task_id))
        conn.commit()
    finally:
        conn.close()
    out = core.reopen_task_api(task_id, {'status': 'running'})
    assert out['ok'] is True
    conn = core.kb.connect(board=board)
    try:
        row = conn.execute('SELECT status,completed_at FROM tasks WHERE id=?', (task_id,)).fetchone()
        assert row['status'] == 'running'
        assert row['completed_at'] is None
        comment = conn.execute('SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1', (task_id,)).fetchone()
        assert 'Reopened from done to running via Cockpit.' in comment['body']
    finally:
        conn.close()


def test_pane_header_has_complete_and_reopen_actions():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'async function reopenTask(task)' in html
    assert "fetch('/reopen/'+encodeURIComponent(task)" in html
    assert "body:JSON.stringify({status:'running'})" in html
    assert '↻ Running' in html
    assert '✓ Complete' in html
