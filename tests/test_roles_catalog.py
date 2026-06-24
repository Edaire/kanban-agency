import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_roles_catalog_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_roles_tab_is_role_definition_catalog_not_session_history():
    core = load_core()
    html = core._cockpit_html('__all__')
    start = html.index('function renderRoleSide()')
    end = html.index('function pruneRecent()', start)
    block = html[start:end]
    assert 'Roles <span class="small">definitions</span>' in block
    assert 'class="role-card ${pc}"' in block
    assert 'role-desc' in block
    assert 'click · drag' in block
    assert 'role-logo' in block
    assert 'providerClass(rr.provider)' in block
    assert 'Role sessions' not in block
    assert 'roleSessions' not in block
    assert 'roleSessionContract' not in block
    assert "att?'🔔'" not in block
    assert 'roleLabel(r)' not in block
    assert "document.querySelectorAll('.chip,.role-card')" in html
    assert "showRoleDetails(b.dataset.role)" in html
    assert "if(b.dataset.role)openRole" not in html
    assert "application/x-kanban-agency-role" in html


def test_role_detail_modal_shows_metadata_and_provider_branding():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'function showRoleDetails(role)' in html
    assert 'role-detail-grid' in html
    assert 'provider-codex' in html
    assert 'provider-claude' in html
    assert 'provider-hermes' in html
    assert 'Rules' in html
    assert 'Aliases' in html


def test_roles_yaml_metadata_is_exposed(tmp_path):
    core = load_core()
    cfg = tmp_path / 'roles.yaml'
    cfg.write_text('''roles:\n  default:\n    provider: hermes\n  analyst:\n    title: Analyst\n    description: Clarify requirements.\n    provider: codex\n    aliases: [analyst]\n''')
    roles, _ = core.load_roles(cfg)
    assert roles['analyst'].title == 'Analyst'
    assert roles['analyst'].description == 'Clarify requirements.'
