import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_board_create_under_test', path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cockpit_has_create_board_button_and_dialog():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'function showBoardDialog()' in html
    assert 'async function createBoard()' in html
    assert 'title="Create Kanban"' in html
    assert 'Kanbans <span' in html
    assert "fetch('/boards'" in html
