import importlib.util
import json
import socket
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_board_archive_under_test', path)
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


def test_archive_board_api_marks_board_archived_and_sessions_all_hides_it(env):
    core = load_core()
    workdir = env / 'analysis'
    workdir.mkdir()
    core.kb.create_board('old_board', name='Old Board', default_workdir=str(workdir))
    core.create_task_api('old_board', {'title': 'Old flow', 'mode': 'workflow'})

    before = core.sessions_all()
    assert any(b['board'] == 'old_board' for b in before['boards'])

    out = core.archive_board_api({'board': 'old_board'})
    assert out['ok'] is True
    assert out['board']['archived'] is True
    assert core.kb.read_board_metadata('old_board')['archived'] is True

    after = core.sessions_all()
    assert not any(b['board'] == 'old_board' for b in after['boards'])
    assert not any(r.get('board') == 'old_board' for r in after['roots'])


def test_gateway_archive_board_endpoint(env):
    core = load_core()
    port = free_port()
    workdir = env / 'analysis'
    workdir.mkdir()
    core.kb.create_board('to_archive', name='To Archive', default_workdir=str(workdir))
    out = core.codex_web_gateway_start(port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        status, body = http_request(port, '/boards/archive', method='POST', body={'board': 'to_archive'})
        assert status == 200
        data = json.loads(body)
        assert data['ok'] is True
        assert data['board']['archived'] is True
    finally:
        core.codex_web_gateway_stop()


def test_cockpit_exposes_drag_to_archive_controls(env):
    core = load_core()
    html = core._cockpit_html('__all__')
    assert 'id="archiveDrop"' in html
    assert 'Drag kanban here to archive' in html
    assert 'data-board-drag=' in html
    assert 'application/x-kanban-agency-board' in html
    assert "fetch('/boards/archive'" in html
    assert 'setupArchiveDrop()' in html
