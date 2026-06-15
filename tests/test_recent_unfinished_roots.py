import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_recent_unfinished_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_recent_includes_all_unfinished_roots_before_done_fillers():
    core = load_core()
    html = core._cockpit_html('__all__')
    start = html.index('function rootUnfinished(root)')
    end = html.index('function renderSessionSide()', start)
    block = html[start:end]
    assert "roles.some(r=>!['done','archived'].includes(r.task_status||''))" in block
    assert 'const unfinished=sorted.filter(rootUnfinished)' in block
    assert 'const done=sorted.filter(r=>!rootUnfinished(r))' in block
    assert 'const recent=[...unfinished,...done.slice(0,Math.max(0,5-unfinished.length))]' in block
    assert 'unfinished roots' in block
