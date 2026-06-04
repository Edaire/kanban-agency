import importlib.util
import sys
from pathlib import Path

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_real_tmux_view_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not Path('/opt/homebrew/bin/tmux').exists() and not Path('/usr/bin/tmux').exists(), reason='tmux not installed')
def test_task_view_reads_real_tmux_capture_and_escapes_html(tmp_path, monkeypatch):
    core = load_core()
    session = 'kanban-test-view-real'
    marker = 'KANBAN_VIEW_REAL_MARKER_<escaped>&ok'
    import subprocess
    subprocess.run(['tmux', 'kill-session', '-t', session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        subprocess.run(['tmux', 'new-session', '-d', '-s', session, 'bash', '-lc', f'printf %s {marker!r}; sleep 30'], check=True)
        state_path = core._codex_web_state_path('realview')
        core._write_json_file(state_path, {'tmux_name': session})
        text = core.task_view_text('realview')
        html = core.task_view_html('realview')
        assert marker in text
        assert 'KANBAN_VIEW_REAL_MARKER_' in html
        assert '&lt;escaped&gt;&amp;ok' in html
        assert '<escaped>&ok' not in html
    finally:
        subprocess.run(['tmux', 'kill-session', '-t', session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
