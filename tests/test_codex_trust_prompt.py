import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_trust', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_wait_for_tui_ready_accepts_codex_trust_prompt(monkeypatch):
    core = load_core()
    calls = []
    captures = iter([
        'Do you trust the contents of this directory?\n› 1. Yes, continue\n  2. No, quit',
        '╭──╮\n│ >_ OpenAI Codex │\n╰──╯\n› ',
    ])
    monkeypatch.setattr(core, '_tmux_capture', lambda *a, **k: next(captures))
    monkeypatch.setattr(core.time, 'sleep', lambda *_: None)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    screen = core._wait_for_tui_ready('kanban-codex-test', timeout=2)
    assert 'OpenAI Codex' in screen
    assert ['tmux', 'send-keys', '-t', 'kanban-codex-test', '1', 'Enter'] in calls
