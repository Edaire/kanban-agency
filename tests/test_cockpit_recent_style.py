import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_recent_style_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_recent_and_kanbans_are_visually_separated():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert '.recent-workset{' in html
    assert 'background:#2d1b3d' in html
    assert '.kanbans-group{border-top:2px solid #334155' in html
    assert '<div class="board-group kanbans-group"><div class="board-title">Kanbans</div>' in html
