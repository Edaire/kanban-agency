import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_recent_sort_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_recent_sort_does_not_mutate_sessions_roots_order():
    core = load_core()
    html = core._cockpit_html('__all__')
    start = html.index('function renderRecentWorkset()')
    end = html.index('function renderSessionSide()', start)
    block = html[start:end]
    assert 'const recent=[...(sessions.roots||[])].sort' in block
    assert 'const recent=(sessions.roots||[]).sort' not in block
