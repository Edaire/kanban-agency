import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_role_roots_in_kanbans_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_kanbans_tab_uses_generic_roots_and_roles_tab_can_stay_launcher_only():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert "function renderSessionSide(){let html='<div class=\"board-group\"><div class=\"board-title\">Kanbans</div>'; let lastBoard=null; let currentBoardEmpty=true; for(const root of sessions.roots)" in html
    assert "function isRoleSessionRoot(root)" not in html
    assert "function rootKey(root){return (root.board||'')+'/'+(root.root_id||root.title)}" in html
