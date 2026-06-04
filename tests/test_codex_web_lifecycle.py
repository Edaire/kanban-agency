import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_codex_web_under_test', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    hermes = home / '.hermes'
    home.mkdir()
    hermes.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(hermes))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def make_codex_task(core, board, workdir, status='running'):
    core.kb.create_board(board, default_workdir=workdir)
    conn = core.kb.connect(board=board)
    try:
        root = core.kb.create_task(conn, title='Root', body='@kanban-agency', created_by='test', initial_status='running', workspace_path=workdir)
        body = core._make_role_card_body(root, 'developer', 'codex', workdir, 'Work', 'instruction')
        task_id = core.kb.create_task(conn, title='[agency] developer: Work', body=body, assignee='agency-developer', created_by='test', parents=[root], initial_status='running', workspace_path=workdir)
        with core.kb.write_txn(conn):
            core._set_status(conn, task_id, status)
        return task_id
    finally:
        conn.close()


def test_codex_web_rebuilds_stopped_session_with_readonly_ttyd(env, monkeypatch):
    core = load_core()
    board = 'codex_web_rebuild'
    workdir = str(env)
    task_id = make_codex_task(core, board, workdir)
    core._write_json_file(core._codex_web_state_path(task_id), {
        'task_id': task_id,
        'board': board,
        'mode': 'native-tmux',
        'state': 'stopped',
        'pid': 111,
        'tmux_name': 'kanban-codex-' + task_id,
        'thread_id': 'native-thread',
        'cwd': workdir,
        'url': 'http://127.0.0.1:1/',
    })

    popens = []
    runs = []
    monkeypatch.setattr(core.shutil, 'which', lambda name: f'/bin/{name}')
    monkeypatch.setattr(core, '_pid_alive', lambda pid: False)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    monkeypatch.setattr(core, '_free_port', iter([42001, 42002]).__next__)
    monkeypatch.setattr(core, '_load_bridge_state', lambda task_id: {})

    def fake_run(cmd, **kwargs):
        runs.append(cmd)
        return SimpleNamespace(returncode=0)

    class FakePopen:
        _pid = 6000
        def __init__(self, cmd, **kwargs):
            type(self)._pid += 1
            self.pid = type(self)._pid
            popens.append(cmd)

    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    monkeypatch.setattr(core.subprocess, 'Popen', FakePopen)

    out = core.codex_web(board, task_id, reuse=False)
    assert out['ok'] is True
    state = out['state']
    assert state['thread_id'] == 'native-thread'
    assert state['url'] == 'http://127.0.0.1:42001/'
    assert state['readonly_url'] == 'http://127.0.0.1:42002/'
    assert state['pid'] != state['readonly_pid']
    assert len(popens) == 2
    assert '--writable' in popens[0]
    assert '--writable' not in popens[1]
    assert all('ttyd-wheel-index.html' in ' '.join(map(str, cmd)) for cmd in popens)
    assert not any(cmd[:3] == ['tmux', 'new-session', '-d'] for cmd in runs), 'tmux is already alive; should only attach ttyd'


def test_codex_web_resumes_tmux_when_stopped_but_thread_exists(env, monkeypatch):
    core = load_core()
    board = 'codex_web_resume_tmux'
    workdir = str(env)
    task_id = make_codex_task(core, board, workdir)
    core._write_json_file(core._codex_web_state_path(task_id), {
        'task_id': task_id,
        'board': board,
        'tmux_name': 'kanban-codex-' + task_id,
        'thread_id': 'resume-thread',
        'cwd': workdir,
    })

    runs = []
    monkeypatch.setattr(core.shutil, 'which', lambda name: f'/bin/{name}')
    monkeypatch.setattr(core, '_pid_alive', lambda pid: False)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: False)
    monkeypatch.setattr(core, '_free_port', iter([43001, 43002]).__next__)
    monkeypatch.setattr(core, '_load_bridge_state', lambda task_id: {})

    def fake_run(cmd, **kwargs):
        runs.append(cmd)
        return SimpleNamespace(returncode=0)

    class FakePopen:
        _pid = 7000
        def __init__(self, cmd, **kwargs):
            type(self)._pid += 1
            self.pid = type(self)._pid

    monkeypatch.setattr(core.subprocess, 'run', fake_run)
    monkeypatch.setattr(core.subprocess, 'Popen', FakePopen)

    out = core.codex_web(board, task_id, reuse=False)
    assert out['ok'] is True
    new_session = next(cmd for cmd in runs if cmd[:3] == ['tmux', 'new-session', '-d'])
    assert 'exec codex resume resume-thread' in ' '.join(new_session)
    assert out['state']['readonly_url'] == 'http://127.0.0.1:43002/'


def test_codex_web_reuse_requires_existing_tmux_alive(env, monkeypatch):
    core = load_core()
    board = 'codex_web_reuse_guard'
    workdir = str(env)
    task_id = make_codex_task(core, board, workdir)
    core._write_json_file(core._codex_web_state_path(task_id), {
        'task_id': task_id,
        'board': board,
        'pid': 999,
        'tmux_name': 'kanban-codex-' + task_id,
        'thread_id': 'thread-x',
        'cwd': workdir,
        'url': 'http://127.0.0.1:999/',
        'cmd': ['ttyd', '--writable', '-t', 'scrollback=50000', 'tmux', 'attach-session'],
    })

    monkeypatch.setattr(core.shutil, 'which', lambda name: f'/bin/{name}')
    monkeypatch.setattr(core, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: False)
    monkeypatch.setattr(core, '_free_port', iter([44001, 44002]).__next__)
    monkeypatch.setattr(core, '_load_bridge_state', lambda task_id: {})

    popen_count = {'n': 0}
    class FakePopen:
        _pid = 8000
        def __init__(self, cmd, **kwargs):
            type(self)._pid += 1
            self.pid = type(self)._pid
            popen_count['n'] += 1

    monkeypatch.setattr(core.subprocess, 'run', lambda cmd, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(core.subprocess, 'Popen', FakePopen)

    out = core.codex_web(board, task_id, reuse=True)
    assert out['ok'] is True
    assert out.get('reused') is not True
    assert popen_count['n'] == 2
    assert out['state']['url'] == 'http://127.0.0.1:44001/'
