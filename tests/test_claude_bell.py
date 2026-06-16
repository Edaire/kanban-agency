import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_claude_bell_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_claude_attention_detects_ready_prompt(monkeypatch):
    core = load_core()
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'tmux': 'kanban-claude-t1'})
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    monkeypatch.setattr(core.subprocess, 'check_output', lambda cmd, text=True, stderr=None: 'done\n────────────────\n❯ \n  ? for shortcuts\n')
    out = core._claude_attention_status('t1')
    assert out['pending'] is True
    assert out['kind'] == 'waiting_for_input'


def test_claude_attention_detects_permission_dialog(monkeypatch):
    core = load_core()
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'tmux': 'kanban-claude-t1'})
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    monkeypatch.setattr(core.subprocess, 'check_output', lambda cmd, text=True, stderr=None: 'Do you want to proceed?\n❯ 1. Yes\n  2. No\n')
    out = core._claude_attention_status('t1')
    assert out['pending'] is True
    assert out['kind'] == 'permission_prompt'


def test_claude_attention_running_when_no_prompt(monkeypatch):
    core = load_core()
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'tmux': 'kanban-claude-t1'})
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    monkeypatch.setattr(core.subprocess, 'check_output', lambda cmd, text=True, stderr=None: '⏺ Reading files...\n● Running tests\n')
    out = core._claude_attention_status('t1')
    assert out['pending'] is False


def test_claude_attention_prompt_wins_over_stale_busy_marker(monkeypatch):
    core = load_core()
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'tmux': 'kanban-claude-t1'})
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    screen = '''⏺ 已打开本地浏览器到 http://localhost:37702/。

────────────────────────────────────────────────
❯ 
────────────────────────────────────────────────
  ? for shortcuts
'''
    monkeypatch.setattr(core.subprocess, 'check_output', lambda cmd, text=True, stderr=None: screen)
    out = core._claude_attention_status('t1')
    assert out['pending'] is True
    assert out['kind'] == 'waiting_for_input'


def test_hermes_attention_detects_ready_prompt(monkeypatch):
    core = load_core()
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'tmux': 'kanban-hermes-t1'})
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    screen = '''done
────────────────────────────────────────────────
❯ 
────────────────────────────────────────────────
  ? for shortcuts
'''
    monkeypatch.setattr(core.subprocess, 'check_output', lambda cmd, text=True, stderr=None: screen)
    out = core._hermes_attention_status('t1')
    assert out['pending'] is True
    assert out['kind'] == 'waiting_for_input'


def test_hermes_attention_running_when_no_prompt(monkeypatch):
    core = load_core()
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'tmux': 'kanban-hermes-t1'})
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    monkeypatch.setattr(core.subprocess, 'check_output', lambda cmd, text=True, stderr=None: '● Working...\nreading files\n')
    out = core._hermes_attention_status('t1')
    assert out['pending'] is False


def test_hermes_attention_does_not_bell_for_interrupt_prompt_while_busy(monkeypatch):
    core = load_core()
    monkeypatch.setattr(core, '_read_json_file', lambda path: {'tmux': 'kanban-hermes-t1'})
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    screen = '''📦 Preflight compression: ~258,195 tokens >= 256,000 threshold. This may take a moment.
🗜️ Compacting context — summarizing earlier conversation so I can continue...

 ⚕ gpt-5.5 · 50% · 🗜️ 12 · 6.0d
─────────────────────────────────────────────────────────────────────
⚕ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel
─────────────────────────────────────────────────────────────────────
'''
    monkeypatch.setattr(core.subprocess, 'check_output', lambda cmd, text=True, stderr=None: screen)
    out = core._hermes_attention_status('t1')
    assert out['pending'] is False
    assert out['kind'] == 'busy_interruptible'
