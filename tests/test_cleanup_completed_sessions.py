import importlib.util
import json
import sys
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_cleanup_completed_under_test', path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cleanup_completed_sessions_stops_old_done_provider_state(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    hermes = home / '.hermes'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(hermes))
    core = load_core()
    board = 'cleanup_board'
    core.kb.create_board(board, name='Cleanup Board', default_workdir=str(tmp_path))
    conn = core.kb.connect(board=board)
    try:
        tid = core.kb.create_task(
            conn,
            title='old done role',
            body='@kanban-agency-role\nrole: tester\nprovider: codex\n',
            assignee='agency-tester',
            created_by='test',
            workspace_kind='dir',
            workspace_path=str(tmp_path),
            initial_status='running',
        )
        old_completed = 1_700_000_000
        conn.execute("UPDATE tasks SET status='done', created_at=?, started_at=?, completed_at=? WHERE id=?", (old_completed, old_completed, old_completed, tid))
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (old_completed, tid))
        conn.commit()
    finally:
        conn.close()

    state_path = core._codex_web_state_path(tid)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({'task_id': tid, 'tmux_name': f'kanban-codex-{tid}', 'pid': 123, 'readonly_pid': 124}), encoding='utf-8')
    # Provider state mtime participates in the Recent activity timestamp; make
    # it old so the test isolates comment/activity semantics.
    import os
    os.utime(state_path, (old_completed, old_completed))

    monkeypatch.setattr(core, '_pid_alive', lambda pid: bool(pid))
    monkeypatch.setattr(core, '_terminate_pid', lambda pid, timeout=2.0: True)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    killed = []

    class CP:
        returncode = 0

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ['tmux', 'kill-session']:
            killed.append(cmd[-1])
        return CP()

    monkeypatch.setattr(core.subprocess, 'run', fake_run)

    conn = core.kb.connect(board=board)
    try:
        # Keep the completion timestamp old enough to qualify, then add a recent
        # comment. Cleanup must use the same Recent activity calculation, so this
        # task should be preserved.
        conn.execute('INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)', (tid, 'user', 'recent follow-up', old_completed + 4 * 86400))
        conn.commit()
    finally:
        conn.close()

    out = core.cleanup_completed_sessions(max_age_days=3, now=old_completed + 4 * 86400, dry_run=False)
    assert out['ok'] is True
    assert out['stopped'] == []
    assert killed == []

    # Once the last activity also falls outside the three-day window, cleanup stops it.
    out = core.cleanup_completed_sessions(max_age_days=3, now=old_completed + 8 * 86400, dry_run=False)
    assert out['ok'] is True
    assert [x['task_id'] for x in out['stopped']] == [tid]
    assert killed == [f'kanban-codex-{tid}']
    updated = json.loads(state_path.read_text())
    assert updated['state'] == 'stopped'
    assert updated['stop_reason'] == 'completed>3d'
