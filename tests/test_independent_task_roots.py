import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_independent_roots_under_test', path)
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
    monkeypatch.setenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', '1')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_independent_task_is_rendered_as_root_with_role_subtask(env, monkeypatch):
    core = load_core()
    board = 'independent_as_root'
    core.kb.create_board(board, name='Independent As Root')
    conn = core.kb.connect(board=board)
    try:
        task_id = core.kb.create_task(
            conn,
            title='[agency] assistant: 完成项目参数的填写',
            body='@kanban-agency-independent\n@kanban-agency-role\nrole: assistant\nprovider: codex\nworkdir: /tmp\nroot_title: 完成项目参数的填写\n',
            assignee='agency-assistant',
            created_by='test',
            initial_status='running',
        )
    finally:
        conn.close()
    monkeypatch.setattr(core, 'session_alert_status', lambda board, task_id: {'pending_approval': False, 'pending': {'pending': False}, 'live': False})

    data = core.sessions_status(board)
    root = next(r for r in data['roots'] if r['root_id'] == task_id)
    assert root['title'] == '完成项目参数的填写'
    assert root['independent'] is True
    assert len(root['roles']) == 1
    assert root['roles'][0]['role'] == 'assistant'
    assert root['roles'][0]['display_title'] == '完成项目参数的填写'


def test_role_label_displays_role_not_independent_title_for_subtasks(env):
    core = load_core()
    html = core._cockpit_html('__all__')
    assert "function roleLabel(r){return `${esc(r.role||'session')}" in html
    role_label_start = html.index("function roleLabel")
    all_roles_start = html.index("function allRoles")
    assert "r.display_title||r.title" not in html[role_label_start:all_roles_start]
