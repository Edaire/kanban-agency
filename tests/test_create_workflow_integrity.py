import importlib.util
import sqlite3
import sys
import time
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_create_integrity_under_test', path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_create_workflow_closes_root_connection_before_advance(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    hermes = home / '.hermes'
    workdir = tmp_path / 'project'
    workdir.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(hermes))
    monkeypatch.setenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', '1')
    core = load_core()
    board = 'integrity_create_board'
    core.kb.create_board(board, name='Integrity Create Board', default_workdir=str(workdir))

    out = core.create_task_api(board, {'title': 'root', 'mode': 'workflow', 'body': 'body'})
    assert out['ok'] is True

    db = hermes / 'kanban' / 'boards' / board / 'kanban.db'
    con = sqlite3.connect(db)
    try:
        assert con.execute('pragma integrity_check').fetchone()[0] == 'ok'
        rows = con.execute('select id,title,status from tasks order by created_at,id').fetchall()
        assert len(rows) == 5
        assert any(r[1] == 'root' for r in rows)
        assert all(r[0] for r in rows)
    finally:
        con.close()
