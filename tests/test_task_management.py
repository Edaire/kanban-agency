import importlib.util
import json
import socket
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_task_management_under_test', path)
    assert spec is not None and spec.loader is not None
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
    monkeypatch.setenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', '1')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def http_request(port, path, *, method='GET', body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = Request(f'http://127.0.0.1:{port}{path}', data=data, headers=headers, method=method)
    with urlopen(req, timeout=5) as r:
        return r.status, r.read().decode('utf-8', errors='replace')


def wait_ready(port):
    for _ in range(50):
        try:
            status, body = http_request(port, '/healthz')
            if status == 200 and 'ok' in body:
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise AssertionError('gateway did not become ready')


def test_create_workflow_task_uses_board_workdir_and_precreates_roles(env):
    core = load_core()
    workdir = env / 'analysis'
    workdir.mkdir()
    core.kb.create_board('analysis', name='ANALYSIS', default_workdir=str(workdir))

    out = core.create_task_api('analysis', {'title': '本地文件流程', 'mode': 'workflow'})

    assert out['ok'] is True
    assert out['mode'] == 'workflow'
    root_id = out['task_id']
    assert out['advance']['advanced'][0]['root_id'] == root_id
    conn = core.kb.connect(board='analysis')
    try:
        root = conn.execute('select * from tasks where id=?', (root_id,)).fetchone()
        assert root['title'] == '本地文件流程'
        assert root['workspace_path'] == str(workdir)
        assert '@kanban-agency' in root['body']
        roles = conn.execute("select title, body, workspace_path from tasks where title like '[agency] %' order by created_at,id").fetchall()
        assert sorted(r['title'].split()[1].rstrip(':') for r in roles) == ['analyst', 'architect', 'developer', 'tester']
        assert all(r['workspace_path'] == str(workdir) for r in roles)
    finally:
        conn.close()


def test_create_independent_task_requires_and_uses_role(env):
    core = load_core()
    workdir = env / 'analysis'
    workdir.mkdir()
    core.kb.create_board('analysis', name='ANALYSIS', default_workdir=str(workdir))

    bad = core.create_task_api('analysis', {'title': '查一下', 'mode': 'independent'})
    assert bad['ok'] is False
    assert 'role is required' in bad['error']

    out = core.create_task_api('analysis', {'title': '查一下', 'mode': 'independent', 'role': 'tester'})
    assert out['ok'] is True
    assert out['mode'] == 'independent'
    conn = core.kb.connect(board='analysis')
    try:
        row = conn.execute('select * from tasks where id=?', (out['task_id'],)).fetchone()
        assert row['title'] == '[agency] tester: 查一下'
        assert row['workspace_path'] == str(workdir)
        assert 'role: tester' in row['body']
        assert '@kanban-agency-independent' in row['body']
        assert 'root_id:' not in row['body']
    finally:
        conn.close()


def test_gateway_create_task_and_cockpit_exposes_task_dialog(env):
    core = load_core()
    port = free_port()
    workdir = env / 'analysis'
    workdir.mkdir()
    core.kb.create_board('analysis', name='ANALYSIS', default_workdir=str(workdir))
    out = core.codex_web_gateway_start(port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        status, body = http_request(port, '/tasks', method='POST', body={'board': 'analysis', 'title': 'Smoke', 'mode': 'workflow'})
        assert status == 200
        created = json.loads(body)
        assert created['ok'] is True
        assert created['mode'] == 'workflow'
        status, html = http_request(port, '/cockpit')
        assert status == 200
        assert 'showTaskDialog(' in html
        assert 'id="taskDialog"' in html
        assert 'Task type' in html
        assert 'Four-role workflow' in html
        assert 'Independent role task' in html
        assert "fetch('/tasks'" in html
    finally:
        core.codex_web_gateway_stop()
