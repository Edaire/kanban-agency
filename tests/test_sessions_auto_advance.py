import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_auto_advance_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Conn:
    def execute(self, sql, params=()):
        class Rows:
            def fetchall(self): return []
        return Rows()
    def close(self): pass


def test_sessions_status_auto_starts_ready_role_before_render(monkeypatch):
    core = load_core()
    calls = []
    monkeypatch.setattr(core.kb, 'board_exists', lambda board: True)
    monkeypatch.setattr(core, 'run', lambda board, task_id=None: calls.append((board, task_id)) or {'board': board, 'started': [{'task_id': task_id}], 'errors': []})
    monkeypatch.setattr(core, '_role_rows', lambda conn, task_id=None: ['ready-row', 'running-row'])
    monkeypatch.setattr(core.kb.Task, 'from_row', lambda row: SimpleNamespace(id='t_ready' if row == 'ready-row' else 't_running', status='ready' if row == 'ready-row' else 'running', body='provider: codex\nrole: developer'))
    monkeypatch.setattr(core.kb, 'connect', lambda board=None: Conn())

    out = core.sessions_status('board1')

    assert out['ok'] is True
    assert calls == [('board1', 't_ready')]
    assert out['auto_advance']['ready_task_ids'] == ['t_ready']


def test_sessions_status_auto_start_throttled(monkeypatch):
    core = load_core()
    calls = []
    monkeypatch.setattr(core.kb, 'board_exists', lambda board: True)
    monkeypatch.setattr(core, 'run', lambda board, task_id=None: calls.append((board, task_id)) or {'board': board, 'started': [{'task_id': task_id}], 'errors': []})
    monkeypatch.setattr(core, '_role_rows', lambda conn, task_id=None: ['ready-row'])
    monkeypatch.setattr(core.kb.Task, 'from_row', lambda row: SimpleNamespace(id='t_ready', status='ready', body='provider: codex\nrole: developer'))
    monkeypatch.setattr(core.time, 'time', lambda: 1000.0)
    monkeypatch.setattr(core.kb, 'connect', lambda board=None: Conn())

    core.sessions_status('board1')
    core.sessions_status('board1')

    assert calls == [('board1', 't_ready')]
