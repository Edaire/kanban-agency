import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_roles_compact_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_role_cards_are_compact_for_single_panel():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert '.role-card{margin:.38em 0 .62em;padding:.38em .54em' in html
    assert 'inline-size:1.35em;block-size:1.35em' in html
    assert '-webkit-line-clamp:1' in html
    assert 'font-size:.92em' in html
    assert 'click · drag' in html
