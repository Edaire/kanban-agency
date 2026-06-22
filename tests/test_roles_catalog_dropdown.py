from pathlib import Path


def test_new_task_independent_role_dropdown_uses_roles_catalog():
    source = Path(__file__).resolve().parents[1] / 'core.py'
    html = source.read_text()
    assert 'function roleOptionsHtml(selected){const catalog=roleCatalog();' in html
    assert 'function syncTaskRoleOptions(){const sel=document.getElementById(\'newTaskRole\')' in html
    assert 'syncTaskRoleOptions();seedRecentFromPanes();' in html
    # The create-task role selector should not be a stale hardcoded subset that
    # diverges from the Roles tab catalog.
    assert '<option value="analyst">analyst</option><option value="architect">architect</option>' not in html
