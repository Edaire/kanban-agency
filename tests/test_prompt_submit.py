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


def test_prompt_missing_from_codex_idle_placeholder_is_detected(tmp_path):
    core = load_core()
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('需要做的任务是：完成项目参数的填写\n请按工作规则做最小必要操作。', encoding='utf-8')
    screen = '''
╭──────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.139.0)                   │
╰──────────────────────────────────────────────╯

› Find and fix a bug in @filename

  gpt-5.5 medium · ~/code/analysis
'''
    assert core._prompt_missing_from_input(screen, prompt) is True


def test_prompt_visible_counts_task_marker_as_unsubmitted(tmp_path):
    core = load_core()
    prompt = tmp_path / 'prompt.md'
    prompt.write_text('需要做的任务是：完成项目参数的填写\n工作目录：/tmp\n请按工作规则做最小必要操作。', encoding='utf-8')
    screen = '› 需要做的任务是：完成项目参数的填写\n工作目录：/tmp\n请按工作规则做最小必要操作。'
    assert core._prompt_still_visible(screen, prompt) is True
    assert core._prompt_missing_from_input(screen, prompt) is False
