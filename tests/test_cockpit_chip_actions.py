import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_chip_actions_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_role_chips_remain_drag_only_without_inline_complete_action():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert "function paneAction(r)" in html
    assert "data-complete-task" not in html
    assert "document.querySelectorAll('.chip,.role-card')" in html
    assert "b.onclick=e=>{e.preventDefault();if(b.classList.contains('role-card')&&b.dataset.role)showRoleDetails(b.dataset.role);}" in html
    assert "completeTask('${esc(r.task_id)}')" in html
