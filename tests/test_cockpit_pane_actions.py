import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_pane_actions_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_complete_action_is_on_pane_header_not_left_chip():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert "function paneAction(r)" in html
    assert "function paneHeader(i,r)" in html
    assert "✓ Complete" in html
    assert "class=\"pane-action\"" in html
    assert "completeTask('${esc(r.task_id)}')" in html
    assert "function roleLabel(r){return `${esc(r.role||'session')}${paneRef(r.task_id)}`}" in html
    assert "data-complete-task" not in html
    assert "chip-action" not in html
