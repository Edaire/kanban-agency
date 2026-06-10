import importlib.util
import json
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_codex_pending_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text('\n'.join(json.dumps(r) for r in records) + '\n', encoding='utf-8')


def test_custom_tool_call_without_output_requires_attention(monkeypatch, tmp_path):
    core = load_core()
    session_file = tmp_path / 'rollout-thread.jsonl'
    write_jsonl(session_file, [
        {
            'timestamp': '2026-06-10T07:53:47.374Z',
            'type': 'response_item',
            'payload': {
                'type': 'custom_tool_call',
                'status': 'completed',
                'call_id': 'call_patch',
                'name': 'apply_patch',
                'input': '*** Begin Patch\n*** Add File: demo.txt\n',
            },
        },
    ])
    monkeypatch.setattr(core, '_find_codex_session_file', lambda thread_id: session_file)

    pending = core._codex_live_pending_approval('thread')

    assert pending['pending'] is True
    assert pending['kind'] == 'tool_call_approval_required'
    assert pending['call_id'] == 'call_patch'
    assert pending['name'] == 'apply_patch'
    assert 'demo.txt' in pending['justification']


def test_custom_tool_call_output_clears_attention(monkeypatch, tmp_path):
    core = load_core()
    session_file = tmp_path / 'rollout-thread.jsonl'
    write_jsonl(session_file, [
        {
            'timestamp': '2026-06-10T07:53:47.374Z',
            'type': 'response_item',
            'payload': {
                'type': 'custom_tool_call',
                'status': 'completed',
                'call_id': 'call_patch',
                'name': 'apply_patch',
                'input': '*** Begin Patch\n*** Add File: demo.txt\n',
            },
        },
        {
            'timestamp': '2026-06-10T07:53:50.000Z',
            'type': 'response_item',
            'payload': {
                'type': 'custom_tool_call_output',
                'call_id': 'call_patch',
                'output': 'Exit code: 0',
            },
        },
    ])
    monkeypatch.setattr(core, '_find_codex_session_file', lambda thread_id: session_file)

    pending = core._codex_live_pending_approval('thread')

    assert pending['pending'] is False
    assert pending['reason'] == 'no_pending_approval'
