import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_writable_link_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cockpit_pane_header_is_directly_editable_not_readonly():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'Open writable' not in html
    assert 'readonly</span>' not in html
    assert 'r.ttyd_url||r.url' in html
