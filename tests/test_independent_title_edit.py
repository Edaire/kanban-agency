import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_title_under_test', path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_independent_display_title_does_not_use_agent_result():
    core = load_core()
    assert core._independent_role_display_title('missing-task', 'researcher', '[agency] researcher: independent chat', 'Agent newest answer should not become title') == '空白会话'


def test_cockpit_has_independent_rename_action():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert "async function renameTask" in html
    assert "/tasks/'+encodeURIComponent(task)+'/title" in html
    assert "✎ Title" in html
    assert "function rootTitleAction(root)" not in html
    assert "class=\"title-edit\"" not in html


def test_update_task_title_api_syncs_kanban_db(tmp_path, monkeypatch):
    core = load_core()
    home = tmp_path / 'home'
    hermes = home / '.hermes'
    home.mkdir(); hermes.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(hermes))
    board = core.INDEPENDENT_ROLE_BOARD
    core.kb.create_board(board, name='Independent Role Chats')
    conn = core.kb.connect(board=board)
    try:
        task_id = core.kb.create_task(conn, title='[agency] researcher: independent chat', body='@kanban-agency-role\nrole: researcher\nprovider: codex\n@kanban-agency-independent')
    finally:
        conn.close()
    out = core.update_task_title_api(task_id, {'title': 'Obsidian 调研'})
    assert out['ok'] is True
    conn = core.kb.connect(board=board)
    try:
        row = conn.execute('SELECT title FROM tasks WHERE id=?', (task_id,)).fetchone()
        assert row['title'] == '[agency] researcher: Obsidian 调研'
    finally:
        conn.close()
