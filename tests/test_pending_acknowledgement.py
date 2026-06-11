import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_pending_ack_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_done_task_suppresses_any_provider_pending_bell():
    core = load_core()
    assert core._provider_pending_acknowledged(
        'done',
        None,
        {},
        {
            'pending': True,
            'kind': 'approval_required',
            'timestamp': '2030-01-01T00:00:00.000Z',
            'cmd': 'docker build ...',
        },
    ) is True


def test_running_task_keeps_provider_pending_attention():
    core = load_core()
    assert core._provider_pending_acknowledged(
        'running',
        9999999999,
        {},
        {
            'pending': True,
            'kind': 'role_completed_waiting_complete',
            'completed_at': 200,
        },
    ) is False


def test_ack_timestamp_can_clear_historical_done_without_completed_at():
    core = load_core()
    assert core._provider_pending_acknowledged(
        'done',
        None,
        {'completion_acknowledged_at': 300},
        {
            'pending': True,
            'kind': 'role_completed_waiting_complete',
            'completed_at': 200,
        },
    ) is True
