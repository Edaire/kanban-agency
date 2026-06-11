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
    spec = importlib.util.spec_from_file_location('ka_core_board_management_under_test', path)
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


def test_gateway_can_create_board_with_required_default_workdir(env):
    core = load_core()
    port = free_port()
    workdir = env / 'analysis'
    workdir.mkdir()
    out = core.codex_web_gateway_start(port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        status, body = http_request(port, '/boards', method='POST', body={
            'slug': 'analysis',
            'name': 'Analysis',
            'workdir': str(workdir),
        })
        assert status == 200
        created = json.loads(body)['board']
        assert created['slug'] == 'analysis'
        assert created['default_workdir'] == str(workdir)

        status, body = http_request(port, '/boards')
        assert status == 200
        boards = json.loads(body)['boards']
        assert any(b['slug'] == 'analysis' and b['default_workdir'] == str(workdir) for b in boards)
    finally:
        core.codex_web_gateway_stop()


def test_gateway_rejects_board_create_without_absolute_workdir(env):
    core = load_core()
    port = free_port()
    out = core.codex_web_gateway_start(port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        req = Request(
            f'http://127.0.0.1:{port}/boards',
            data=json.dumps({'slug': 'bad', 'workdir': 'relative/path'}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with pytest.raises(HTTPError) as excinfo:
            urlopen(req, timeout=5)
        assert excinfo.value.code == 400
    finally:
        core.codex_web_gateway_stop()


def test_cockpit_exposes_board_management_controls(env):
    core = load_core()
    html = core._cockpit_html('__all__')

    assert 'id="boardManager"' not in html
    assert '<button id="tabSessions" class="sideTab active" onclick="setSideMode(\'sessions\')">Kanbans</button>' in html
    assert 'showBoardDialog()' not in html
    assert 'Kanbans <span style="float:right" onclick="event.stopPropagation();showBoardDialog()">+</span>' not in html
    assert 'collapsedKanbans' in html
    assert 'id="boardDialog"' not in html
    assert 'newBoardSlug' not in html
    assert 'createBoard()' not in html
    assert "location.href='/cockpit/'+encodeURIComponent(data.board.slug)" not in html
    assert "querySelectorAll('#layouts .layoutBtn')" in html
    assert "querySelectorAll('.layoutBtn')" not in html
