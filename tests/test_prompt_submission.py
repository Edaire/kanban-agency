import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_prompt_submit_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_ensure_prompt_submitted_sends_extra_enter_when_prompt_visible(monkeypatch, tmp_path):
    core = load_core()
    calls = []
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('工作目录：/tmp\n需要做的功能是：demo\n请按工作规则执行。')

    screens = iter([
        '› 工作目录：/tmp\n  需要做的功能是：demo\n  请按工作规则执行。\n\n  gpt-5.5 medium',
        '• Working (1s • esc to interrupt)',
    ])

    def fake_check_output(cmd, text=True, stderr=None):
        return next(screens)

    def fake_run(cmd, check=False):
        calls.append(cmd)
        return None

    monkeypatch.setattr(core.subprocess, 'check_output', fake_check_output)
    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    out = core._ensure_prompt_submitted('tmux-session', prompt, attempts=2, delay=0)

    assert out['submitted'] is True
    assert out['extra_enter_sent'] == 1
    assert any(cmd[-1] == 'Enter' for cmd in calls)


def test_ensure_prompt_submitted_noops_when_prompt_not_visible(monkeypatch, tmp_path):
    core = load_core()
    calls = []
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('工作目录：/tmp\n需要做的功能是：demo\n请按工作规则执行。')
    monkeypatch.setattr(core.subprocess, 'check_output', lambda cmd, text=True, stderr=None: '• Working (12s • esc to interrupt)')
    monkeypatch.setattr(core.subprocess, 'run', lambda cmd, check=False: calls.append(cmd))

    out = core._ensure_prompt_submitted('tmux-session', prompt, attempts=1, delay=0)

    assert out['submitted'] is True
    assert out['extra_enter_sent'] == 0
    assert calls == []
