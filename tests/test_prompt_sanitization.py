import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_prompt_sanitization_under_test', path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_extract_root_task_body_strips_kanban_directives():
    core = load_core()
    body = '''@kanban-agency
workdir: /tmp/project
workflow: functional-development

Build the feature.
Keep this line.'''
    assert core._extract_root_task_body(body) == 'Build the feature.\nKeep this line.'


def test_role_card_body_does_not_put_control_marker_in_root_task_body():
    core = load_core()
    body = core._make_role_card_body('root1', 'analyst', 'codex', '/tmp/project', 'Build feature', 'Clarify scope')
    task_text = core._extract_root_task_body(body)
    assert task_text == 'Clarify scope'
    fenced = body.split('root_task_body:\n```text\n', 1)[1].split('\n```', 1)[0]
    assert '@kanban-agency' not in fenced
    assert 'workdir:' not in fenced


def test_codex_prompt_does_not_show_marker_as_task_description():
    core = load_core()
    class Task:
        title = '[agency] analyst: clarify - Empty root'
        body = core._make_role_card_body('root1', 'analyst', 'codex', '/tmp/project', 'Empty root', '@kanban-agency\nworkdir: /tmp/project')
    prompt = core._codex_prompt(Task(), {'role': 'analyst', 'root_title': 'Empty root', 'workdir': '/tmp/project'})
    assert '任务描述：\n@kanban-agency' not in prompt
    assert '需要做的功能是：Empty root' in prompt
