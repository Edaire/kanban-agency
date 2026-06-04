import importlib.util
import os
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_workflow_under_test', path)
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
    monkeypatch.chdir(tmp_path)
    return tmp_path


def make_root_with_role(core, board, role='analyst', role_status='done'):
    kb = core.kb
    kb.create_board(board)
    conn = kb.connect(board=board)
    try:
        root_body = '@kanban-agency\nworkdir: /tmp/work\n\nFeature root'
        root = kb.create_task(conn, title='Feature X', body=root_body, created_by='test', initial_status='running')
        body = core._make_role_card_body(root, role, 'codex', '/tmp/work', 'Feature X', 'instruction')
        task = kb.create_task(conn, title=f'[agency] {role}: Feature X', body=body, assignee=core._agency_assignee(role), created_by='test', parents=[root], initial_status='running')
        with kb.write_txn(conn):
            core._set_status(conn, task, role_status, result='done')
        return root, task
    finally:
        conn.close()


def titles(core, board):
    conn = core.kb.connect(board=board)
    try:
        return [r['title'] for r in conn.execute('select title from tasks order by created_at,id').fetchall()]
    finally:
        conn.close()


def test_advance_dry_run_finds_next_role_after_done_previous(env):
    core = load_core()
    board = 'workflow_next_role'
    root, _ = make_root_with_role(core, board, role='analyst', role_status='done')
    out = core.advance(board, root_id=root, dry_run=True)
    assert not out['errors']
    wf = out['advanced'][0]['workflow']
    created_roles = [x['role'] for x in wf['created']]
    assert created_roles == ['architect', 'developer', 'tester']
    assert wf['created'][0]['title'] == '[agency] architect: design - Feature X'


def test_advance_is_idempotent_when_next_role_already_exists(env):
    core = load_core()
    board = 'workflow_no_duplicate'
    root, _ = make_root_with_role(core, board, role='analyst', role_status='done')
    conn = core.kb.connect(board=board)
    try:
        body = core._make_role_card_body(root, 'architect', 'codex', '/tmp/work', 'Feature X', 'instruction')
        core.kb.create_task(conn, title='[agency] architect: design - Feature X', body=body, assignee='agency-architect', created_by='test', parents=[root], initial_status='running')
    finally:
        conn.close()
    out = core.advance(board, root_id=root, dry_run=True)
    wf = out['advanced'][0]['workflow']
    created_roles = [x['role'] for x in wf['created']]
    assert created_roles == ['developer', 'tester']
    assert any(x['role'] == 'architect' for x in wf['reused'])


def test_workflow_watch_once_dry_run_combines_monitor_and_advance(env):
    core = load_core()
    board = 'workflow_watch_once'
    root, _ = make_root_with_role(core, board, role='analyst', role_status='done')
    out = core.workflow_watch(board, once=True, dry_run=True)
    assert not out['errors']
    adv = out['iterations'][0]['advance']
    assert adv['advanced'][0]['root_id'] == root
    assert [x['role'] for x in adv['advanced'][0]['workflow']['created']] == ['architect', 'developer', 'tester']
