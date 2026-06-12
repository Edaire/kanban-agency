import importlib.util
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_codex_thread_recovery_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_recover_codex_thread_by_matching_tmux_tail_to_jsonl(monkeypatch, tmp_path):
    core = load_core()
    home = tmp_path / 'home'
    sessions = home / '.codex' / 'sessions' / '2026' / '06' / '12'
    sessions.mkdir(parents=True)
    thread = '019eb0de-e7b0-7ca3-b3eb-ebc3ac845bdf'
    fp = sessions / f'rollout-2026-06-10T17-30-58-{thread}.jsonl'
    fp.write_text('{"payload":{"cwd":"/repo"}}\n复验结果：仍不通过，不建议提交\n收到，这个也应记录为前端验收问题\n', encoding='utf-8')
    monkeypatch.setattr(core.Path, 'home', lambda: home)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    monkeypatch.setattr(core.subprocess, 'check_output', lambda *a, **k: '复验结果：仍不通过，不建议提交\n收到，这个也应记录为前端验收问题\n› Use /skills to list available skills')
    writes = []
    monkeypatch.setattr(core, '_write_json_file', lambda path, data: writes.append(data))
    out = core._recover_codex_thread_for_task('task-x', {'tmux_name': 'tmux-x', 'cwd': '/repo', 'started_at': 0})
    assert out == thread
    assert writes[-1]['thread_id'] == thread
    assert writes[-1]['thread_recovery_method'] == 'tmux_tail_jsonl_overlap'


def test_no_codex_tmux_text_bell_fallback_left():
    core = load_core()
    html_or_src = Path(__file__).resolve().parents[1].joinpath('core.py').read_text()
    assert '_codex_tmux_attention_status' not in html_or_src
    assert 'codex_permission_prompt' not in html_or_src
