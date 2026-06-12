import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_pane_action_colors_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_pane_action_complete_and_reopen_have_distinct_colors():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert '.pane-action.complete{color:#bfdbfe' in html
    assert 'border-color:#3b82f6' in html
    assert '.pane-action.reopen{color:#bbf7d0' in html
    assert 'border-color:#22c55e' in html
    assert 'class="pane-action complete" title="Complete in Kanban"' in html
    assert 'class="pane-action reopen" title="Reopen as running"' in html
