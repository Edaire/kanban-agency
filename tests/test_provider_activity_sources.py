import importlib.util
import os
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_provider_activity_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    hermes = home / '.hermes'
    codex = home / '.codex' / 'sessions' / '2026' / '06' / '15'
    home.mkdir(); hermes.mkdir(); codex.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(hermes))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def touch(path: Path, ts: int, content='x'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    os.utime(path, (ts, ts))


def test_provider_activity_reads_codex_jsonl(env):
    core = load_core()
    task = 't_codex'
    thread = '019eca36-0f66-7732-9023-f6e9f2094528'
    touch(Path.home()/'.codex/sessions/2026/06/15'/f'rollout-{thread}.jsonl', 2000000100)
    core._write_json_file(core._codex_web_state_path(task), {'thread_id': thread})
    assert core._provider_activity_at(task, 'codex') == 2000000100


def test_provider_activity_reads_hermes_web_logs(env):
    core = load_core()
    task = 't_hermes'
    touch(Path.home()/'.hermes/kanban-agency/hermes-web'/f'{task}.stderr.log', 2000000200)
    assert core._provider_activity_at(task, 'hermes') == 2000000200


def test_provider_activity_reads_claude_run_logs(env):
    core = load_core()
    task = 't_claude'
    touch(Path.home()/'.hermes/kanban-agency/claude-runs'/task/'session.log', 2000000300)
    assert core._provider_activity_at(task, 'claude') == 2000000300
