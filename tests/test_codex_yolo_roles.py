import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_codex_yolo_under_test', path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_codex_yolo_enabled_for_non_ops_roles():
    core = load_core()
    assert '--dangerously-bypass-approvals-and-sandbox' in core._codex_command({'role': 'developer'})
    assert '--dangerously-bypass-approvals-and-sandbox' in core._codex_command({'role': 'tester'})
    assert '--dangerously-bypass-approvals-and-sandbox' in core._codex_command({'role': 'researcher'})


def test_codex_yolo_disabled_for_ops_roles():
    core = load_core()
    assert '--dangerously-bypass-approvals-and-sandbox' not in core._codex_command({'role': 'ops'})
    assert '--dangerously-bypass-approvals-and-sandbox' not in core._codex_command({'role': 'operator'})


def test_codex_resume_preserves_yolo_policy():
    core = load_core()
    assert core._codex_command({'role': 'developer'}, resume_thread_id='abc') == 'exec codex --dangerously-bypass-approvals-and-sandbox resume abc'
    assert core._codex_command({'role': 'ops'}, resume_thread_id='abc') == 'exec codex resume abc'
