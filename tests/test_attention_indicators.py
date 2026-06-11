import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_attention_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cockpit_roles_tab_shows_attention_count_and_role_group_bell():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'function roleAttention()' in html
    assert "r.innerHTML='Roles'+(ra?` 🔔 ${ra}`:'')" in html
    assert "s.innerHTML='Kanbans'+(ka?` 🔔 ${ka}`:'')" in html
    assert "const att=roleRoot.attention||0" in html
    assert "${att?'🔔':(rr.active?'●':'○')}" in html
