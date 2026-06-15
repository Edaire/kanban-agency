import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_role_config_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_update_role_config_writes_yaml_and_backup(tmp_path, monkeypatch):
    core = load_core()
    cfg = tmp_path / 'roles.yaml'
    cfg.write_text('''roles:\n  default:\n    provider: hermes\n  developer:\n    title: Developer\n    description: Old\n    provider: codex\n    rules: []\n    aliases: [developer]\n''', encoding='utf-8')
    monkeypatch.setattr(core, 'CONFIG_PATH', cfg)
    data = core.update_role_config_api({
        'role': 'developer',
        'title': 'Dev',
        'description': 'New desc',
        'provider': 'claude',
        'rules': ['/tmp/nope.md'],
        'aliases': ['dev', '开发'],
    }, path=cfg)
    assert data['ok'] is True
    assert data['backup']
    assert Path(data['backup']).exists()
    roles, _ = core.load_roles(cfg)
    assert roles['developer'].title == 'Dev'
    assert roles['developer'].description == 'New desc'
    assert roles['developer'].provider == 'claude'
    assert roles['developer'].rules == ['/tmp/nope.md']
    assert roles['developer'].aliases == ['dev', '开发']
    assert data['warnings'] == ['rule file not found: /tmp/nope.md']


def test_role_editor_frontend_and_endpoint_are_present():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'function showRoleEditor(role)' in html
    assert 'function saveRoleConfig(role)' in html
    assert "fetch('/roles/config'" in html
    assert 'roleEditRules' in html
    core.codex_web_gateway_stop()
    out = core.codex_web_gateway_start(port=0)
    script = Path(out['state']['script']).read_text()
    core.codex_web_gateway_stop()
    assert "if path == '/roles/config':" in script
    assert 'core.update_role_config_api(payload)' in script
