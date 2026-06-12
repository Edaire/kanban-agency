import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_independent_titles_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_clean_independent_title_removes_role_prefix_and_generic_chat():
    core = load_core()
    assert core._clean_independent_title('[agency] developer: 文件抽屉改为右侧工作区分栏', 'developer') == '文件抽屉改为右侧工作区分栏'
    assert core._clean_independent_title('[agency] ops: independent chat', 'ops') == '空白会话'


def test_independent_title_can_derive_short_summary_from_result():
    core = load_core()
    assert core._summary_title_from_result('已把“文件抽屉”改成右侧工作区分栏，不再是覆盖式 Drawer') == '文件抽屉改为右侧工作区分栏'
    assert core._summary_title_from_result('Claude interactive tmux session started: http://127.0.0.1:1/s/x') == 'Claude 交互会话'
