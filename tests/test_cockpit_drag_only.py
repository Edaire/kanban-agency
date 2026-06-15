import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_drag_only_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cockpit_sidebar_chips_are_drag_only_not_click_to_open_pane():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert "b.onclick=e=>{e.preventDefault();if(b.classList.contains('role-card')&&b.dataset.role)showRoleDetails(b.dataset.role);}" in html
    assert "setPane(active,b.dataset.task)" not in html
    assert "openRole(b.dataset.role,b.dataset.board)" not in html
    assert "e.dataTransfer.setData('text/plain', b.dataset.task)" in html
    assert "application/x-kanban-agency-role" in html
