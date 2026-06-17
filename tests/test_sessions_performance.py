import importlib.util
import json
import sys
import time
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_perf_under_test', path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_codex_session_file_lookup_builds_single_index(monkeypatch, tmp_path):
    core = load_core()
    root = tmp_path / '.codex' / 'sessions' / '2026' / '06'
    root.mkdir(parents=True)
    tid1 = '11111111-1111-4111-8111-111111111111'
    tid2 = '22222222-2222-4222-8222-222222222222'
    p1 = root / f'{tid1}.jsonl'
    p2 = root / f'session-{tid2}.jsonl'
    p1.write_text('{}\n')
    p2.write_text('{}\n')
    monkeypatch.setattr(core.Path, 'home', lambda: tmp_path)

    calls = []
    original_rglob = core.Path.rglob

    def counted_rglob(self, pattern):
        calls.append((str(self), pattern))
        return original_rglob(self, pattern)

    monkeypatch.setattr(core.Path, 'rglob', counted_rglob)
    assert core._find_codex_session_file(tid1) == p1
    assert core._find_codex_session_file(tid2) == p2
    assert core._find_codex_session_file(tid1) == p1
    assert calls == [(str(tmp_path / '.codex' / 'sessions'), '*.jsonl')]


def test_codex_pending_approval_uses_file_signature_cache(monkeypatch, tmp_path):
    core = load_core()
    tid = '33333333-3333-4333-8333-333333333333'
    path = tmp_path / f'{tid}.jsonl'
    path.write_text(json.dumps({'payload': {'type': 'function_call', 'call_id': 'c1', 'arguments': json.dumps({'sandbox_permissions': 'require_escalated', 'cmd': 'date'})}}) + '\n')
    monkeypatch.setattr(core, '_find_codex_session_file', lambda thread_id: path)

    read_count = {'n': 0}
    original_read_text = core.Path.read_text

    def counted_read_text(self, *args, **kwargs):
        if self == path:
            read_count['n'] += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(core.Path, 'read_text', counted_read_text)
    first = core._codex_live_pending_approval(tid)
    second = core._codex_live_pending_approval(tid)
    assert first['pending'] is True
    assert second['pending'] is True
    assert read_count['n'] == 1

    time.sleep(0.001)
    path.write_text(path.read_text() + json.dumps({'payload': {'type': 'function_call_output', 'call_id': 'c1'}}) + '\n')
    third = core._codex_live_pending_approval(tid)
    assert third['pending'] is False
    assert read_count['n'] >= 2
