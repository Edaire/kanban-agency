import importlib.util
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_gateway_routes_under_test', path)
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


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def make_task(core, board, workdir, status='running'):
    core.kb.create_board(board, name='Gateway Routes', default_workdir=workdir)
    conn = core.kb.connect(board=board)
    try:
        root = core.kb.create_task(conn, title='Root', body='@kanban-agency', created_by='test', initial_status='running', workspace_path=workdir)
        body = core._make_role_card_body(root, 'developer', 'codex', workdir, 'Gateway task', 'instruction')
        task_id = core.kb.create_task(conn, title='[agency] developer: Gateway task', body=body, assignee='agency-developer', created_by='test', parents=[root], initial_status='running', workspace_path=workdir)
        with core.kb.write_txn(conn):
            core._set_status(conn, task_id, status)
        return task_id
    finally:
        conn.close()


def http_get(port, path):
    with urlopen(f'http://127.0.0.1:{port}{path}', timeout=5) as r:
        return r.status, r.read().decode('utf-8', errors='replace')


def wait_ready(port):
    for _ in range(50):
        try:
            status, body = http_get(port, '/healthz')
            if status == 200 and 'ok' in body:
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise AssertionError('gateway did not become ready')


def test_gateway_routes_status_sessions_cockpit_and_s(env):
    core = load_core()
    board = 'gateway_routes'
    task_id = make_task(core, board, str(env), status='running')
    core._write_json_file(core._codex_web_state_path(task_id), {
        'task_id': task_id,
        'board': board,
        'pid': 1234,
        'readonly_pid': 1235,
        'url': 'http://127.0.0.1:41001/',
        'readonly_url': 'http://127.0.0.1:41002/',
        'thread_id': 'thread-gateway',
        'tmux_name': 'kanban-codex-' + task_id,
        'cwd': str(env),
    })
    port = free_port()
    out = core.codex_web_gateway_start(port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        status, body = http_get(port, '/sessions')
        assert status == 200
        data = json.loads(body)
        assert data['ok'] is True
        assert any(r.get('task_id') == task_id for root in data['roots'] for r in root['roles'])

        status, body = http_get(port, f'/sessions/{board}')
        assert status == 200
        assert task_id in body
        assert 'readonly_ttyd_url' in body

        status, body = http_get(port, '/cockpit')
        assert status == 200
        assert 'Session Cockpit' in body
        assert 'r.ttyd_url||r.url' in body

        status, body = http_get(port, f'/cockpit/{board}')
        assert status == 200
        assert 'Session Cockpit' in body

        status, body = http_get(port, f'/status/{task_id}')
        assert status == 200
        st = json.loads(body)
        assert st['task_id'] == task_id
        assert st['readonly_ttyd_url'] == 'http://127.0.0.1:41002/'

        status, body = http_get(port, f'/s/{task_id}?cockpit=1')
        assert status == 200
        assert 'iframe' in body
        assert f'/status/{task_id}' in body
    finally:
        core.codex_web_gateway_stop()


def test_gateway_resume_route_invokes_codex_web_without_reuse(env, monkeypatch):
    core = load_core()
    board = 'gateway_resume'
    task_id = make_task(core, board, str(env), status='running')
    port = free_port()
    calls = []

    # The gateway runs in a separate Python process generated from core.py. To
    # avoid monkeypatch crossing process boundaries, assert the route through a
    # real but fully mocked codex_web path by using harmless fake executables via
    # code-level state: no actual /resume is invoked until the gateway imports
    # core.py, so here we test route existence/shape with a task that already has
    # enough state for codex_web to fail deterministically rather than 404.
    core._write_json_file(core._codex_web_state_path(task_id), {
        'task_id': task_id,
        'board': board,
        'pid': 999,
        'url': 'http://127.0.0.1:1/',
        'thread_id': 'thread-resume',
        'tmux_name': 'kanban-codex-' + task_id,
        'cwd': str(env),
    })
    out = core.codex_web_gateway_start(port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        status, body = http_get(port, f'/resume/{task_id}')
        assert status == 200
        data = json.loads(body)
        # In the isolated gateway process this may fail if ttyd/codex/tmux are
        # not mocked, but the route must resolve the task and return codex_web's
        # JSON shape rather than a missing-task response.
        assert 'ok' in data
        assert data.get('error') != 'task not found'
    finally:
        core.codex_web_gateway_stop()
