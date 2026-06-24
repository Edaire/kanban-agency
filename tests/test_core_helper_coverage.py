import importlib.util
import json
import sys
from collections import OrderedDict
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_helper_coverage_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    hermes = home / '.hermes'
    home.mkdir()
    hermes.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(hermes))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_roles(path, roles):
    path.write_text(json.dumps({'roles': roles}), encoding='utf-8')


def test_load_roles_validates_required_shape_and_role_fields(env):
    core = load_core()
    path = env / 'roles.json'

    path.write_text('[]', encoding='utf-8')
    with pytest.raises(ValueError, match="top-level 'roles'"):
        core.load_roles(path)

    path.write_text(json.dumps({'roles': []}), encoding='utf-8')
    with pytest.raises(ValueError, match='roles must be a mapping'):
        core.load_roles(path)

    write_roles(path, {'bad-key': {'provider': 'codex'}, 'default': {'provider': 'codex'}})
    with pytest.raises(ValueError, match='invalid role key'):
        core.load_roles(path)

    write_roles(path, {'default': {'provider': 'bad'}})
    with pytest.raises(ValueError, match='invalid provider'):
        core.load_roles(path)

    write_roles(path, {'default': {'provider': 'codex', 'rules': 'nope'}})
    with pytest.raises(ValueError, match='rules must be a string array'):
        core.load_roles(path)

    write_roles(path, {'default': {'provider': 'codex', 'aliases': 'nope'}})
    with pytest.raises(ValueError, match='aliases must be a string array'):
        core.load_roles(path)

    write_roles(path, {
        'default': {'provider': 'codex', 'rules': None, 'aliases': None},
        'ops': {'provider': 'claude', 'rules': ['ops.md'], 'aliases': ['Operations'], 'title': 'Ops', 'description': 'Runbooks'},
    })
    roles, warnings = core.load_roles(path)
    assert warnings == []
    assert list(roles) == ['default', 'ops']
    assert roles['default'].rules == []
    assert roles['default'].aliases == []
    assert roles['ops'].title == 'Ops'


def test_workdir_rule_and_role_matching_helpers_cover_warning_paths(env, monkeypatch):
    core = load_core()
    workdir = env / 'workspace'
    workdir.mkdir()
    missing = env / 'missing'

    body = f'workdir: relative\nworkdir: {missing}\n'
    got, warnings = core._resolve_workdir(body, 'board')
    assert got is None
    assert any('multiple workdir lines' in w for w in warnings)
    assert any('invalid non-absolute workdir' in w for w in warnings)

    monkeypatch.setattr(core.kb, 'read_board_metadata', lambda board: {'default_workdir': str(missing)})
    got, warnings = core._resolve_workdir('', 'board')
    assert got == str(missing)
    assert any('board default_workdir does not exist' in w for w in warnings)

    monkeypatch.setattr(core.kb, 'read_board_metadata', lambda board: {'default_workdir': 'relative'})
    got, warnings = core._resolve_workdir('', 'board')
    assert got is None
    assert any('invalid board default_workdir' in w for w in warnings)

    role = core.Role('developer', 'codex', ['missing.md', str(workdir / 'abs-missing.md')], ['Dev', 'dev', ''])
    warnings = core._rule_warnings(role, None)
    assert any('without workdir' in w for w in warnings)
    warnings = core._rule_warnings(role, str(workdir))
    assert len(warnings) == 2

    aliases = core._dedup_aliases(role)
    assert aliases == ['developer', 'Dev']

    roles = OrderedDict([
        ('default', core.Role('default', 'codex', [], ['fallback'])),
        ('developer', core.Role('developer', 'codex', [], ['dev'])),
        ('tester', core.Role('tester', 'codex', [], ['qa'])),
    ])
    assert core.match_roles('Need dev', '', roles)[1] == 'developer'
    matched, route, _ = core.match_roles('Need dev and qa', '', roles)
    assert matched == ['developer', 'tester']
    assert route == 'default'
    assert core.match_roles('fallback please', '', roles)[0] == ['default']
    assert core.match_roles('unknown', '', roles) == ([], 'default', [])


def test_role_body_parse_session_path_and_termination_helpers(env, monkeypatch):
    core = load_core()
    root = {'root_id': 'root1', 'title': 'Root title', 'body': 'Root body', 'workdir': str(env)}
    role = core.Role('developer', 'codex', ['dev.md'], ['dev'])
    body = core._role_body(root, role)
    assert '@kanban-agency-role' in body
    assert 'role: developer' in body
    assert core._parse_role_body(body)['root_id'] == 'root1'
    assert core._parse_role_rules(body) == ['dev.md']
    assert core._parse_role_rules('rules:\n- (none)\n') == []
    assert core._agency_assignee(' Developer ') == 'agency-developer'
    assert core._agency_assignee('') is None

    assert core._session_cwd_compatible({'cwd': str(env)}, str(env)) is True
    assert core._session_cwd_compatible({'cwd': '/definitely/missing'}, str(env)) is False
    monkeypatch.setattr(core.Path, 'resolve', lambda self: (_ for _ in ()).throw(OSError('bad path')))
    assert core._session_cwd_compatible({'cwd': '/a'}, '/a') is True
    assert core._session_cwd_compatible({'cwd': '/a'}, '/b') is False

    paths = core._state_paths_for_task('t_123')
    assert len({str(p) for p in paths}) == len(paths)
    assert any('codex-web' in str(p) for p in paths)

    assert core._terminate_pid(None) is False
    assert core._terminate_pid('not-int') is False
    monkeypatch.setattr(core, '_pid_alive', lambda pid: False)
    assert core._terminate_pid(1234) is False

