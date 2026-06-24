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

    assert 'class="role-card ${pc}"' in html
    assert 'data-role="${esc(rr.role)}"' in html
    assert 'data-task="${esc(r.task_id||\'\')}"' in html
    assert 'class="chip role-def' not in html


def test_cockpit_sidebar_labels_roles_and_sessions():
    core = load_core()
    html = core._cockpit_html('__all__')

    assert 'id="tabRoles"' in html
    assert 'id="tabSessions"' in html
    assert 'Role Catalog' not in html


def test_cockpit_defaults_independent_task_group_collapsed():
    core = load_core()
    html = core._cockpit_html('some_board')

    assert "root.collapsed" in html
    assert "collapsed&&!expandedRoots.has(key)" in html



def test_cockpit_left_sidebar_tabs_roles_and_sessions_without_touching_panes():
    core = load_core()
    html = core._cockpit_html('some_board')

    assert 'id="tabSessions"' in html
    assert 'id="tabRoles"' in html
    assert "let sideMode='sessions'" in html
    assert 'function setSideMode' in html
    assert "sideMode==='roles'?renderRoleSide():renderSessionSide()" in html
    assert 'function renderPanes' in html



def test_cockpit_does_not_special_case_independent_role_roots():
    core = load_core()
    html = core._cockpit_html('__all__')

    assert 'function isRoleSessionRoot(root)' not in html
    assert 'function roleSessionRoots' not in html
    assert "String(root.root_id||'').startsWith('role:')" not in html
    start = html.index('function renderRoleSide()')
    end = html.index('function pruneRecent()', start)
    block = html[start:end]
    assert 'roleSessionRoots' not in block
    assert 'Role sessions' not in block
    assert 'data-task="${esc(r.task_id||\'\')}"' not in block



def test_sessions_tab_uses_same_role_labels_for_all_roots():
    core = load_core()
    html = core._cockpit_html('__all__')

    assert 'function renderRoleSide' in html
    assert 'function renderSessionSide' in html
    assert '${sym(r.task_status,r.pending_approval)} ${roleLabel(r)}' in html
    assert "${roleRoot?esc(r.display_title||r.role):roleLabel(r)}" not in html


def test_recent_uses_same_collapse_rule_for_all_roots():
    core = load_core()
    html = core._cockpit_html('__all__')

    start = html.index('function renderRecentWorkset()')
    end = html.index('function renderSessionSide()', start)
    block = html[start:end]
    assert "const roleRoot=" not in block
    assert "const collapsed=collapsedRoots.has(key)||(root.collapsed&&!expandedRoots.has(key))" in block
    assert '${sym(r.task_status,r.pending_approval)} ${roleLabel(r)}' in block
