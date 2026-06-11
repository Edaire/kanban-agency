import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_prompt_content_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_codex_prompt_includes_independent_task_title_and_description():
    core = load_core()
    task = SimpleNamespace(
        title='[agency] assistant: 完成项目参数的填写',
        body='''@kanban-agency-role
role: assistant
provider: codex
workdir: /Users/admin/code/analysis
root_title: 完成项目参数的填写

rules:
- /tmp/assistant.md

root_task_body:
```text
@kanban-agency
workdir: /Users/admin/code/analysis

这是用户填写的详细描述。
```

@kanban-agency-independent
''',
    )
    prompt = core._codex_prompt(task, core._parse_role_body(task.body))
    assert '需要做的任务是：完成项目参数的填写' in prompt
    assert '任务描述：' in prompt
    assert '这是用户填写的详细描述。' in prompt
    assert '/tmp/assistant.md' in prompt
