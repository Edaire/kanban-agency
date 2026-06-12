import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_layout_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_two_column_layout_uses_one_to_two_ratio():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert '.layout-2{grid-template-columns:minmax(0,1fr) minmax(0,2fr);grid-template-rows:1fr}' in html
    assert '.layout-2{grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:1fr}' not in html
