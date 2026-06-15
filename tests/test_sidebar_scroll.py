import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_sidebar_scroll_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_sidebar_scrolls_only_sessions_area():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert '.side{border-right:1px solid #26313d;background:#111822;overflow:hidden' in html
    assert 'grid-template-rows:auto minmax(0,1fr) auto' in html
    assert '#sessions{min-height:0;overflow:auto' in html
