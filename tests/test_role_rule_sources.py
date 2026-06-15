import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_rule_source_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_role_rule_sources_api_reads_rule_file(tmp_path):
    core = load_core()
    rule = tmp_path / 'developer.md'
    rule.write_text('old rule body', encoding='utf-8')
    cfg = tmp_path / 'roles.yaml'
    cfg.write_text(f'''roles:\n  default:\n    provider: hermes\n  developer:\n    provider: codex\n    rules:\n      - {rule}\n''', encoding='utf-8')
    data = core.role_rule_sources_api('developer', path=cfg)
    assert data['ok'] is True
    assert data['rules'][0]['path'] == str(rule)
    assert data['rules'][0]['exists'] is True
    assert data['rules'][0]['editable'] is True
    assert data['rules'][0]['content'] == 'old rule body'


def test_update_role_config_writes_rule_source_and_backup(tmp_path, monkeypatch):
    core = load_core()
    rule = tmp_path / 'developer.md'
    rule.write_text('old', encoding='utf-8')
    cfg = tmp_path / 'roles.yaml'
    cfg.write_text(f'''roles:\n  default:\n    provider: hermes\n  developer:\n    title: Developer\n    description: Old\n    provider: codex\n    rules:\n      - {rule}\n    aliases: [developer]\n''', encoding='utf-8')
    monkeypatch.setattr(core, 'CONFIG_PATH', cfg)
    data = core.update_role_config_api({
        'role': 'developer',
        'title': 'Developer',
        'description': 'Old',
        'provider': 'codex',
        'rules': [str(rule)],
        'aliases': ['developer'],
        'rule_contents': {str(rule): 'new body'},
    }, path=cfg)
    assert data['ok'] is True
    assert rule.read_text(encoding='utf-8') == 'new body'
    assert list(tmp_path.glob('developer.md.bak.*'))


def test_role_editor_loads_and_saves_rule_sources():
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'async function loadRoleRuleSources(role)' in html
    assert "fetch('/roles/'+encodeURIComponent(role)+'/rules'" in html
    assert 'textarea[data-rule-path]' in html
    assert 'rule_contents:ruleContents' in html
    core.codex_web_gateway_stop()
    out = core.codex_web_gateway_start(port=0)
    script = Path(out['state']['script']).read_text()
    core.codex_web_gateway_stop()
    assert "path.endswith('/rules')" in script
    assert 'core.role_rule_sources_api(role)' in script
