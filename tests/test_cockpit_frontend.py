import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_frontend_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cockpit_initial_render_waits_for_sessions_data_and_uses_readonly_ttyd():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'let panesRenderedWithData=false' in html
    assert 'if(!panesRenderedWithData){renderPanes(); panesRenderedWithData=true;}' in html
    assert "renderPanes();setInterval(refresh" not in html
    assert 'r.ttyd_url||r.url' in html
    assert '/view/${r.task_id}' not in html


def test_cockpit_has_fixed_dimensions_to_avoid_fit_jitter():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'grid-template-rows:40px minmax(0,1fr)' in html
    assert 'grid-template-rows:32px minmax(0,1fr)' in html
    assert 'scrollbar-gutter:stable' in html
    assert 'flex-wrap:nowrap' in html


def test_cockpit_does_not_rewrite_dom_when_content_is_unchanged():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'let lastSideHtml' in html
    assert 'if(html===lastSideHtml)return' in html
    assert 'h.dataset.last!==next' in html
    assert 'document.title!==nextTitle' in html



def test_cockpit_distinguishes_role_definitions_from_role_sessions():
    core = load_core()
    html = core._cockpit_html('some_board')

    assert 'class="chip role-def ${rr.active?\'active\':\'idle\'}"' in html
    assert "rr.active?'active':'idle'" in html
    assert "data-role" in html
    assert "data-task" in html


def test_cockpit_sidebar_labels_roles_and_sessions():
    core = load_core()
    html = core._cockpit_html('__all__')

    assert '<div class="board-title">Roles</div>' in html
    assert '<div class="board-title">Sessions</div>' in html
    assert 'id="tabRoles"' not in html
    assert 'id="tabSessions"' not in html
    assert 'Role Catalog' not in html


def test_cockpit_defaults_independent_task_group_collapsed():
    core = load_core()
    html = core._cockpit_html('some_board')

    assert "root.collapsed" in html
    assert "collapsed&&!expandedRoots.has(key)" in html



def test_cockpit_left_sidebar_stacks_roles_above_sessions():
    core = load_core()
    html = core._cockpit_html('some_board')

    assert 'function renderRoleSide' in html
    assert 'function renderSessionSide' in html
    assert 'renderRoleSide()+renderSessionSide()' in html
    assert 'let sideMode=' not in html
    assert 'function setSideMode' not in html
