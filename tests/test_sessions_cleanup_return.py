import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_sessions_cleanup_return_under_test', path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_sessions_all_returns_cleanup_status(monkeypatch):
    core = load_core()
    monkeypatch.setattr(core, '_auto_cleanup_completed_sessions', lambda: {'ok': True, 'skipped': 'test'})
    monkeypatch.setattr(core.kb, 'list_boards', lambda include_archived=False: [])
    out = core.sessions_all()
    assert out['ok'] is True
    assert out['completed_session_cleanup'] == {'ok': True, 'skipped': 'test'}
