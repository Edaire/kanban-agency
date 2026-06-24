import importlib.util
import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_remote_gateway_under_test', path)
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


def wait_ready(port):
    for _ in range(60):
        try:
            with urlopen(f'http://127.0.0.1:{port}/healthz', timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.05)
    raise AssertionError('gateway did not become ready')


def request(port, path, *, method='GET', body=None, cookie=None, csrf=None, host=None, origin=None, allow_error=False):
    data = None
    headers = {'Connection': 'close'}
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if csrf:
        headers['X-CSRF-Token'] = csrf
    if host:
        headers['Host'] = host
    if origin:
        headers['Origin'] = origin
    if cookie:
        headers['Cookie'] = cookie
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode('utf-8', errors='replace')
        if resp.status >= 400 and not allow_error:
            raise HTTPError(f'http://127.0.0.1:{port}{path}', resp.status, resp.reason, resp.headers, None)
        return resp.status, text, dict(resp.headers)
    finally:
        conn.close()


def request_form(port, path, form, *, host=None, origin=None):
    data = '&'.join(f'{k}={v}' for k, v in form.items()).encode('utf-8')
    headers = {'Connection': 'close', 'Content-Type': 'application/x-www-form-urlencoded'}
    if host:
        headers['Host'] = host
    if origin:
        headers['Origin'] = origin
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request('POST', path, body=data, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode('utf-8', errors='replace')
        if resp.status >= 400:
            raise HTTPError(f'http://127.0.0.1:{port}{path}', resp.status, resp.reason, resp.headers, None)
        return resp.status, text, dict(resp.headers)
    finally:
        conn.close()


def login(port, secret):
    status, body, headers = request(port, '/login', method='POST', body={'secret': secret}, host=f'127.0.0.1:{port}', origin=f'http://127.0.0.1:{port}')
    cookie = headers['Set-Cookie'].split(';', 1)[0]
    status, body, _ = request(port, '/auth/me', cookie=cookie, host=f'127.0.0.1:{port}')
    assert status == 200
    data = json.loads(body)
    assert data['ok'] is True
    return cookie, data['csrf_token']


def assert_http_error(code, fn):
    with pytest.raises(HTTPError) as exc:
        fn()
    try:
        exc.value.read()
    finally:
        exc.value.close()
    assert exc.value.code == code


def auth_file(tmp_path, port):
    p = tmp_path / 'remote-auth.json'
    p.write_text(json.dumps({
        'readonly_secret': 'read-secret',
        'writable_secret': 'write-secret',
        'allowed_hosts': [f'127.0.0.1:{port}', f'localhost:{port}'],
        'allowed_origins': [f'http://127.0.0.1:{port}', f'http://localhost:{port}'],
    }), encoding='utf-8')
    return p


def auth_file_for_entrypoint(tmp_path, host, origin):
    p = tmp_path / 'remote-auth-entrypoint.json'
    p.write_text(json.dumps({
        'readonly_secret': 'read-secret',
        'writable_secret': 'write-secret',
        'allowed_hosts': [host],
        'allowed_origins': [origin],
    }), encoding='utf-8')
    return p


def start_backend(text):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            body = text.encode('utf-8')
            self.send_response(200)
            self.send_header('content-type', 'text/html; charset=utf-8')
            self.send_header('content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f'http://127.0.0.1:{server.server_address[1]}/'


def start_basic_auth_backend(text, credential):
    seen = []
    expected = 'Basic ' + __import__('base64').b64encode(credential.encode('utf-8')).decode('ascii')

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            seen.append(self.headers.get('Authorization') or '')
            if self.headers.get('Authorization') != expected:
                body = b'auth required'
                self.send_response(401)
                self.send_header('www-authenticate', 'Basic realm="ttyd"')
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = text.encode('utf-8')
            self.send_response(200)
            self.send_header('content-type', 'text/html; charset=utf-8')
            self.send_header('content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f'http://127.0.0.1:{server.server_address[1]}/', seen


def start_reverse_proxy(target_port, listen_port=0):
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            return

        def _forward_http(self):
            body = None
            if self.command in ('POST', 'PUT', 'PATCH'):
                length = int(self.headers.get('Content-Length') or '0')
                body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items()}
            conn = http.client.HTTPConnection('127.0.0.1', target_port, timeout=5)
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                self.send_response(resp.status, resp.reason)
                for k, v in resp.headers.items():
                    if k.lower() in ('transfer-encoding', 'connection'):
                        continue
                    self.send_header(k, v)
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(data)
            finally:
                conn.close()

        def _forward_websocket(self):
            with socket.create_connection(('127.0.0.1', target_port), timeout=5) as upstream:
                upstream.sendall((f'{self.command} {self.path} {self.request_version}\r\n').encode('iso-8859-1'))
                for k, v in self.headers.items():
                    upstream.sendall((f'{k}: {v}\r\n').encode('iso-8859-1'))
                upstream.sendall(b'\r\n')
                while True:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        break
                    self.connection.sendall(chunk)
                    if b'\r\n\r\n' in chunk:
                        break

        def do_GET(self):
            if self.headers.get('Upgrade', '').lower() == 'websocket':
                self._forward_websocket()
                return
            self._forward_http()

        def do_POST(self):
            self._forward_http()

    server = ThreadingHTTPServer(('127.0.0.1', listen_port), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def start_websocket_backend():
    seen = {}
    ready = threading.Event()
    done = threading.Event()
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(('127.0.0.1', 0))
    server_socket.listen(1)
    server_socket.settimeout(10)
    port = server_socket.getsockname()[1]

    def run():
        ready.set()
        try:
            conn, _ = server_socket.accept()
            with conn:
                data = conn.recv(4096)
                seen['request'] = data.decode('iso-8859-1', errors='replace')
                conn.sendall(
                    b'HTTP/1.1 101 Switching Protocols\r\n'
                    b'Upgrade: websocket\r\n'
                    b'Connection: Upgrade\r\n'
                    b'\r\n'
                )
        finally:
            done.set()
            server_socket.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(2)
    return f'http://127.0.0.1:{port}/', seen, done


def make_task(core, board, workdir, *, url='http://127.0.0.1:41001/', readonly_url='http://127.0.0.1:41002/'):
    core.kb.create_board(board, name='Remote Board', default_workdir=workdir)
    conn = core.kb.connect(board=board)
    try:
        root = core.kb.create_task(conn, title='Root', body='@kanban-agency', created_by='test', initial_status='running', workspace_path=workdir)
        with core.kb.write_txn(conn):
            core._set_status(conn, root, 'done')
        body = core._make_role_card_body(root, 'developer', 'codex', workdir, 'Remote task', 'instruction')
        task_id = core.kb.create_task(conn, title='[agency] developer: Remote task', body=body, assignee='agency-developer', created_by='test', parents=[root], initial_status='running', workspace_path=workdir)
        core._write_json_file(core._codex_web_state_path(task_id), {
            'task_id': task_id,
            'board': board,
            'pid': 1234,
            'readonly_pid': 1235,
            'url': url,
            'readonly_url': readonly_url,
            'thread_id': 'thread-remote',
            'tmux_name': 'kanban-codex-' + task_id,
            'cwd': workdir,
        })
        return task_id
    finally:
        conn.close()


def add_backend_credentials(core, task_id, *, writable='gateway-write:secret', readonly='gateway-read:secret'):
    state_path = core._codex_web_state_path(task_id)
    state = core._read_json_file(state_path)
    state['backend_credential'] = writable
    state['readonly_backend_credential'] = readonly
    core._write_json_file(state_path, state)


def install_fake_tmux(tmp_path, monkeypatch, session_name):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / 'fake-tmux.log'
    text_path = tmp_path / 'fake-tmux-text.log'
    script = bin_dir / 'tmux'
    script.write_text(
        '#!/bin/sh\n'
        'echo "$@" >> "$KA_FAKE_TMUX_LOG"\n'
        'if [ "$1" = "list-sessions" ]; then echo "$KA_FAKE_TMUX_SESSION"; exit 0; fi\n'
        'if [ "$1" = "has-session" ]; then exit 0; fi\n'
        'if [ "$1" = "load-buffer" ]; then cat "$4" >> "$KA_FAKE_TMUX_TEXT"; fi\n'
        'exit 0\n',
        encoding='utf-8',
    )
    script.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir) + ':' + __import__('os').environ.get('PATH', ''))
    monkeypatch.setenv('KA_FAKE_TMUX_LOG', str(log_path))
    monkeypatch.setenv('KA_FAKE_TMUX_TEXT', str(text_path))
    monkeypatch.setenv('KA_FAKE_TMUX_SESSION', session_name)
    return log_path, text_path


def test_gateway_remote_startup_requires_explicit_remote_and_auth(env):
    core = load_core()
    assert core.codex_web_gateway_start(host='0.0.0.0', port=free_port())['ok'] is False
    assert core.codex_web_gateway_start(host='127.0.0.1', remote=True, port=free_port())['ok'] is False

    port = free_port()
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        assert out['state']['host'] == '127.0.0.1'
        assert out['state']['remote'] is True
        assert 'HTTPS/TLS' in out['warning']
        assert 'read-secret' not in json.dumps(out)
        wait_ready(port)

        changed = env / 'remote-auth-rotated.json'
        changed.write_text(json.dumps({
            'readonly_secret': 'read-secret-2',
            'writable_secret': 'write-secret-2',
            'allowed_hosts': [f'127.0.0.1:{port}'],
            'allowed_origins': [f'http://127.0.0.1:{port}'],
        }), encoding='utf-8')
        rotated = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(changed), port=port)
        assert rotated['ok'] is False
        assert rotated['code'] == 'gateway_restart_required'
    finally:
        core.codex_web_gateway_stop()


def test_gateway_cli_help_exposes_remote_flags():
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'kanban-agency'
    cp = subprocess.run([sys.executable, str(script), 'codex-web-gateway', '--help'], text=True, capture_output=True, check=True)
    assert '--host' in cp.stdout
    assert '--remote' in cp.stdout
    assert '--auth-file' in cp.stdout


def test_gateway_auth_helpers_validate_loopback_and_auth_files(env):
    core = load_core()
    assert core._gateway_host_is_loopback('') is True
    assert core._gateway_host_is_loopback('localhost') is True
    assert core._gateway_host_is_loopback('::1') is True
    assert core._gateway_host_is_loopback('127.12.0.9') is True
    assert core._gateway_host_is_loopback('0.0.0.0') is False
    assert core._gateway_host_is_loopback('not an ip') is False

    missing = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(env / 'missing.json'), port=free_port())
    assert missing['ok'] is False
    assert missing['code'] == 'remote_auth_required'
    assert set(missing['missing']) == {'readonly_secret', 'writable_secret', 'allowed_hosts', 'allowed_origins'}

    invalid = env / 'invalid-auth.json'
    invalid.write_text('[]', encoding='utf-8')
    assert core._load_gateway_auth_file(str(invalid)) == {}
    assert core._gateway_auth_fingerprint({}) == ''
    assert len(core._gateway_auth_fingerprint({'readonly_secret': 'r'})) == 64


def test_gateway_main_dispatch_reports_start_and_stop_results(env, monkeypatch, capsys):
    core = load_core()

    monkeypatch.setattr(
        core,
        'codex_web_gateway_start',
        lambda **kwargs: {'ok': True, 'kwargs': kwargs},
    )
    assert core.main(['codex-web-gateway', '--host', '0.0.0.0', '--port', '9876', '--remote', '--auth-file', str(env / 'auth.json')]) == 0
    started = json.loads(capsys.readouterr().out)
    assert started['ok'] is True
    assert started['kwargs'] == {
        'host': '0.0.0.0',
        'port': 9876,
        'remote': True,
        'auth_file': str(env / 'auth.json'),
    }

    monkeypatch.setattr(core, 'codex_web_gateway_stop', lambda: {'ok': False, 'error': 'no gateway state'})
    assert core.main(['codex-web-gateway-stop']) == 1
    stopped = json.loads(capsys.readouterr().out)
    assert stopped == {'ok': False, 'error': 'no gateway state'}


@pytest.mark.parametrize(
    ('command', 'patch_name', 'argv', 'expected_args', 'expected_kwargs'),
    [
        ('scan', 'scan', ['scan', '--board', 'b', '--roles', 'roles.yml'], ('b', 'roles.yml'), {}),
        ('start', 'start', ['start', '--board', 'b', '--roles', 'roles.yml'], ('b', 'roles.yml'), {}),
        ('run', 'run', ['run', '--board', 'b', '--listen', 'ws://x', '--dry-run', '--task-id', 't'], ('b',), {'listen': 'ws://x', 'dry_run': True, 'task_id': 't'}),
        ('continue', 'continue_comments', ['continue', '--board', 'b', '--listen', 'ws://x', '--dry-run', '--task-id', 't'], ('b',), {'listen': 'ws://x', 'dry_run': True, 'task_id': 't'}),
        ('sync', 'sync', ['sync', '--board', 'b', '--task-id', 't'], ('b',), {'task_id': 't'}),
        ('advance', 'advance', ['advance', '--board', 'b', '--root-id', 'r', '--dry-run'], ('b',), {'root_id': 'r', 'dry_run': True}),
        ('workflow-watch', 'workflow_watch', ['workflow-watch', '--board', 'b', '--interval', '0.1', '--once', '--dry-run'], ('b',), {'interval': 0.1, 'once': True, 'dry_run': True}),
        ('monitor', 'monitor', ['monitor', '--board', 'b', '--task-id', 't', '--dry-run'], ('b',), {'task_id': 't', 'dry_run': True}),
        ('codex-web', 'codex_web', ['codex-web', '--board', 'b', '--task-id', 't', '--port', '8123', '--no-reuse'], ('b', 't'), {'port': 8123, 'reuse': False}),
        ('codex-web-stop', 'codex_web_stop', ['codex-web-stop', '--board', 'b', '--task-id', 't'], ('b', 't'), {}),
    ],
)
def test_main_dispatches_existing_commands(env, monkeypatch, capsys, command, patch_name, argv, expected_args, expected_kwargs):
    core = load_core()
    seen = {}

    def fake(*args, **kwargs):
        seen['args'] = args
        seen['kwargs'] = kwargs
        return {'ok': True, 'cmd': command, 'errors': []}

    monkeypatch.setattr(core, patch_name, fake)
    assert core.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['cmd'] == command

    assert seen['args'][0] == expected_args[0]
    if len(expected_args) > 1:
        assert str(seen['args'][1]) == expected_args[1]
    assert seen['kwargs'] == expected_kwargs


def test_gateway_stop_handles_missing_state_and_kill_fallback(env, monkeypatch):
    core = load_core()
    assert core.codex_web_gateway_stop() == {'ok': False, 'error': 'no gateway state'}

    core._write_json_file(core.CODEX_WEB_GATEWAY_STATE, {'pid': 4242, 'port': 8766})
    alive_calls = iter([True, False])
    monkeypatch.setattr(core, '_pid_alive', lambda pid: next(alive_calls, False))
    monkeypatch.setattr(core.os, 'killpg', lambda *args: (_ for _ in ()).throw(OSError('no process group')))
    monkeypatch.setattr(core.os, 'kill', lambda *args: (_ for _ in ()).throw(OSError('no process')))

    out = core.codex_web_gateway_stop()
    assert out['ok'] is True
    assert out['state']['state'] == 'stopped'
    assert core._read_json_file(core.CODEX_WEB_GATEWAY_STATE)['state'] == 'stopped'


def test_remote_mode_requires_auth_and_csrf_for_writes(env):
    core = load_core()
    port = free_port()
    task_id = make_task(core, 'remote_authz', str(env))
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, read_csrf = login(port, 'read-secret')
        status, body, _ = request(port, '/sessions', cookie=readonly, host=f'127.0.0.1:{port}')
        assert status == 200
        data = json.loads(body)
        serialized = json.dumps(data)
        assert task_id in serialized
        assert 'http://127.0.0.1:41001/' not in serialized
        assert 'ttyd_url' not in serialized

        assert_http_error(403, lambda: request(port, f'/complete/{task_id}', method='POST', body={}, cookie=readonly, csrf=read_csrf, host=f'127.0.0.1:{port}', origin=f'http://127.0.0.1:{port}'))

        writable, csrf = login(port, 'write-secret')
        assert_http_error(403, lambda: request(port, f'/complete/{task_id}', method='POST', body={}, cookie=writable, host=f'127.0.0.1:{port}', origin=f'http://127.0.0.1:{port}'))

        status, body, _ = request(port, f'/complete/{task_id}', method='POST', body={}, cookie=writable, csrf=csrf, host=f'127.0.0.1:{port}', origin=f'http://127.0.0.1:{port}')
        assert status == 200
        assert json.loads(body)['ok'] is True

        assert_http_error(405, lambda: request(port, f'/resume/{task_id}', cookie=writable, host=f'127.0.0.1:{port}'))
    finally:
        core.codex_web_gateway_stop()


def test_remote_mode_rejects_unauthenticated_reads(env):
    core = load_core()
    port = free_port()
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        assert_http_error(401, lambda: request(port, '/sessions', host=f'127.0.0.1:{port}'))
        status, body, _ = request(port, '/', host=f'127.0.0.1:{port}')
        assert status == 200
        assert 'name="secret"' in body

        status, body, _ = request(port, '/cockpit', host=f'127.0.0.1:{port}')
        assert status == 200
        assert 'name="secret"' in body
    finally:
        core.codex_web_gateway_stop()


def test_remote_ttyd_route_and_write_lease(env):
    core = load_core()
    port = free_port()
    writable_server, writable_url = start_backend('<html><head></head><body>writable backend terminal<script>c={fontSize:13,fontFamily:"mono"}</script></body></html>')
    readonly_server, readonly_url = start_backend('<html><head></head><body>readonly backend terminal<script>c={fontSize:13,fontFamily:"mono"}</script></body></html>')
    task_id = make_task(core, 'remote_ttyd', str(env), url=writable_url, readonly_url=readonly_url)
    add_backend_credentials(core, task_id)
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        assert_http_error(401, lambda: request(port, f'/ttyd/{task_id}', host=f'127.0.0.1:{port}'))

        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, f'/ttyd/{task_id}?port=9', cookie=readonly, host=f'127.0.0.1:{port}')
        assert status == 200
        assert 'readonly backend terminal' in body
        assert str(readonly_server.server_address[1]) not in body
        assert '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">' in body
        assert 'data-kanban-mobile-ttyd' in body
        assert '-webkit-text-size-adjust:100%' in body
        assert 'fontSize:(window.innerWidth<=760?6:13)' in body

        writable_a, csrf_a = login(port, 'write-secret')
        status, body, _ = request(port, f'/ttyd/{task_id}?mode=write', cookie=writable_a, csrf=csrf_a, host=f'127.0.0.1:{port}')
        assert status == 200
        assert 'writable backend terminal' in body

        writable_evil, _ = login(port, 'write-secret')
        assert_http_error(
            403,
            lambda: request(
                port,
                f'/ttyd/{task_id}?mode=write',
                cookie=writable_evil,
                host=f'127.0.0.1:{port}',
                origin='http://evil.example',
            ),
        )

        writable_b, csrf_b = login(port, 'write-secret')
        assert_http_error(409, lambda: request(port, f'/ttyd/{task_id}?mode=write', cookie=writable_b, csrf=csrf_b, host=f'127.0.0.1:{port}'))

        request(port, '/logout', method='POST', body={}, cookie=writable_a, csrf=csrf_a, host=f'127.0.0.1:{port}', origin=f'http://127.0.0.1:{port}')
        status, body, _ = request(port, f'/ttyd/{task_id}?mode=write', cookie=writable_b, csrf=csrf_b, host=f'127.0.0.1:{port}')
        assert status == 200
        assert 'writable backend terminal' in body
    finally:
        core.codex_web_gateway_stop()
        writable_server.shutdown()
        readonly_server.shutdown()


def test_remote_ttyd_requires_backend_credential_and_injects_it(env):
    core = load_core()
    port = free_port()
    backend, backend_url, seen = start_basic_auth_backend('guarded readonly terminal', 'gateway-read:secret')
    task_id = make_task(core, 'remote_ttyd_guarded', str(env), url=backend_url, readonly_url=backend_url)
    state_path = core._codex_web_state_path(task_id)
    state = core._read_json_file(state_path)
    state['readonly_backend_credential'] = 'gateway-read:secret'
    core._write_json_file(state_path, state)
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, f'/ttyd/{task_id}?mode=auto', cookie=readonly, host=f'127.0.0.1:{port}')
        assert status == 200
        assert 'guarded readonly terminal' in body
        assert seen and seen[-1].startswith('Basic ')
        assert 'gateway-read:secret' not in body
    finally:
        backend.shutdown()
        core.codex_web_gateway_stop()


def test_remote_ttyd_rejects_historical_unprotected_backend(env):
    core = load_core()
    port = free_port()
    backend, backend_url = start_backend('unprotected terminal should not proxy')
    task_id = make_task(core, 'remote_ttyd_unprotected', str(env), url=backend_url, readonly_url=backend_url)
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, f'/ttyd/{task_id}?mode=auto', cookie=readonly, host=f'127.0.0.1:{port}', allow_error=True)
        assert status == 409
        data = json.loads(body)
        assert data['code'] == 'ttyd_backend_unprotected'
        assert 'unprotected terminal should not proxy' not in body
    finally:
        backend.shutdown()
        core.codex_web_gateway_stop()


def test_remote_boards_and_board_sessions_are_sanitized(env):
    core = load_core()
    port = free_port()
    task_id = make_task(core, 'remote_sanitize_board', str(env), url='http://127.0.0.1:49101/', readonly_url='http://127.0.0.1:49102/')
    add_backend_credentials(core, task_id)
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, '/boards', cookie=readonly, host=f'127.0.0.1:{port}')
        assert status == 200
        assert str(env) not in body
        assert 'default_workdir' not in body

        status, body, _ = request(port, '/sessions/remote_sanitize_board', cookie=readonly, host=f'127.0.0.1:{port}')
        assert status == 200
        assert task_id in body
        assert 'http://127.0.0.1:49101/' not in body
        assert 'ttyd_url' not in body
        assert str(env) not in body
    finally:
        core.codex_web_gateway_stop()


def test_remote_logout_login_page_resume_and_role_open_routes(env):
    core = load_core()
    port = free_port()
    task_id = make_task(core, 'remote_routes', str(env))
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        status, login_html, _ = request(port, '/login', host=f'127.0.0.1:{port}')
        assert status == 200
        assert 'Content-Type":"application/json' in login_html
        assert 'location.pathname' in login_html
        assert 'location.href="/cockpit"' not in login_html
        assert 'function safeNext(value)' in login_html
        assert 'value.startsWith("//")' in login_html

        status, mobile_login_html, _ = request(port, '/mobile', host=f'127.0.0.1:{port}')
        assert status == 200
        assert 'Kanban Remote Login' in mobile_login_html
        assert 'location.pathname' in mobile_login_html
        assert 'safeNext(q.get("next")||location.pathname)' in mobile_login_html

        readonly, read_csrf = login(port, 'read-secret')
        assert_http_error(403, lambda: request(port, '/logout', method='POST', body={}, cookie=readonly, host=f'127.0.0.1:{port}', origin=f'http://127.0.0.1:{port}'))
        status, body, _ = request(port, '/logout', method='POST', body={}, cookie=readonly, csrf=read_csrf, host=f'127.0.0.1:{port}', origin=f'http://127.0.0.1:{port}')
        assert status == 200
        assert json.loads(body)['ok'] is True

        writable, csrf = login(port, 'write-secret')
        status, body, _ = request(port, f'/resume/{task_id}', method='POST', body={}, cookie=writable, csrf=csrf, host=f'127.0.0.1:{port}', origin=f'http://127.0.0.1:{port}', allow_error=True)
        assert status in (200, 400)
        assert 'ok' in json.loads(body)

        status, body, _ = request(port, '/roles/remote_routes/developer/open', method='POST', body={}, cookie=writable, csrf=csrf, host=f'127.0.0.1:{port}', origin=f'http://127.0.0.1:{port}', allow_error=True)
        assert status in (200, 400)
        assert 'ok' in json.loads(body)
    finally:
        core.codex_web_gateway_stop()


def test_remote_cockpit_requires_allowed_host(env):
    core = load_core()
    port = free_port()
    make_task(core, 'remote_cockpit_host', str(env))
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        cookie, _ = login(port, 'read-secret')
        assert_http_error(403, lambda: request(port, '/cockpit', cookie=cookie, host='evil.example.test'))
    finally:
        core.codex_web_gateway_stop()


def test_remote_cockpit_uses_stable_ttyd_iframe_urls(env):
    core = load_core()
    port = free_port()
    task_id = make_task(core, 'remote_stable_frame', str(env))
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, '/cockpit', cookie=readonly, host=f'127.0.0.1:{port}')
        assert status == 200
        assert "'/ttyd/'+encodeURIComponent(r.task_id)+'?mode=auto'" in body
        assert "url+='&csrf='+encodeURIComponent(remoteMe.csrf_token)" in body
        assert "'/ttyd/'+encodeURIComponent(r.task_id)+'?mode=auto&t='" not in body
        assert 'mode=auto&t=\'+Date.now()' not in body
        assert "frame.getAttribute('src')" in body
        assert 'frame.src!==desired' not in body
    finally:
        core.codex_web_gateway_stop()


def test_remote_cockpit_has_mobile_single_pane_layout(env):
    core = load_core()
    port = free_port()
    make_task(core, 'remote_mobile', str(env))
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, '/cockpit', cookie=readonly, host=f'127.0.0.1:{port}')
        assert status == 200
        assert '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">' in body
        assert '@media(max-width:760px)' in body
        assert 'id="mobileSessionToggle"' in body
        assert 'function setMobileSessionsOpen(open)' in body
        assert "document.body.classList.toggle('mobile-drawer-open'" in body
        assert '.app.mobile-drawer-open .side' in body
        assert '.side{position:fixed' in body
        assert '.pane{display:none;' in body
        assert '.pane.active{display:grid}' in body
        assert 'height:100dvh' in body
        assert '.body,.body iframe{height:100%;min-height:0;max-height:100%;overflow:hidden}' in body
        assert 'setMobileSessionsOpen(false)' in body
        assert 'function setPane(i,task)' in body
        assert 'updatePaneFrames();setMobileSessionsOpen(false);' in body
    finally:
        core.codex_web_gateway_stop()


def test_remote_mobile_page_is_standalone_single_terminal_shell(env):
    core = load_core()
    port = free_port()
    make_task(core, 'remote_mobile_shell', str(env))
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, '/mobile', cookie=readonly, host=f'127.0.0.1:{port}')
        assert status == 200
        assert 'data-shell="remote-mobile"' in body
        assert 'id="taskBoard"' in body
        assert 'id="attentionList"' in body
        assert 'id="runningList"' in body
        assert 'id="recentList"' in body
        assert 'function renderTaskBoard()' in body
        assert 'function showTerminal(task)' in body
        assert 'function canConnect(task)' in body
        assert '!!(task&&task.ttyd_alive)' in body
        assert 'data-task-id="' in body
        assert 'function canResume(task)' in body
        assert 'function resumeTask(task)' in body
        assert 'id="sessionMessage"' in body
        assert 'No connectable session' in body
        assert 'Resume' in body
        assert 'needs writable login' in body
        assert 'Attention' in body
        assert 'Running' in body
        assert 'Recent' in body
        assert 'id="terminalFrame"' in body
        assert 'id="terminalView"' in body
        assert 'id="mobileInputBar"' in body
        assert 'id="mobileInputText"' in body
        assert 'function submitMobileInput()' in body
        assert "fetch('/mobile-input/'" in body
        assert "remoteMe.role==='writable'" in body
        assert 'Readonly or occupied' in body
        assert '@media(max-height:520px)' in body
        assert "fetch('/sessions'" in body
        assert "'/ttyd/'+encodeURIComponent(task.task_id)+'?mode=auto'" in body
        assert "desired+='&csrf='+encodeURIComponent(remoteMe.csrf_token)" in body
        assert "frame.getAttribute('src')" in body
        assert 'selectTask(current)' not in body
        assert 'Session Cockpit' not in body
        assert 'layout-' not in body
        assert 'function renderPanes' not in body
        assert 'kanban-cockpit-state' not in body
    finally:
        core.codex_web_gateway_stop()


def test_remote_mobile_input_posts_to_tmux_with_writable_csrf_and_lease(env, monkeypatch):
    core = load_core()
    port = free_port()
    task_id = make_task(core, 'remote_mobile_input', str(env))
    state = core._read_json_file(core._codex_web_state_path(task_id))
    log_path, text_path = install_fake_tmux(env, monkeypatch, state['tmux_name'])
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, read_csrf = login(port, 'read-secret')
        assert_http_error(
            403,
            lambda: request(
                port,
                f'/mobile-input/{task_id}',
                method='POST',
                body={'text': 'readonly blocked', 'enter': True},
                cookie=readonly,
                csrf=read_csrf,
                host=f'127.0.0.1:{port}',
                origin=f'http://127.0.0.1:{port}',
            ),
        )

        writable_a, csrf_a = login(port, 'write-secret')
        status, body, _ = request(
            port,
            f'/mobile-input/{task_id}',
            method='POST',
            body={'text': 'mobile hello', 'enter': True},
            cookie=writable_a,
            csrf=csrf_a,
            host=f'127.0.0.1:{port}',
            origin=f'http://127.0.0.1:{port}',
        )
        assert status == 200
        assert json.loads(body)['ok'] is True
        assert text_path.read_text(encoding='utf-8') == 'mobile hello'
        log = log_path.read_text(encoding='utf-8')
        assert 'paste-buffer -r -t ' + state['tmux_name'] in log
        assert 'send-keys -t ' + state['tmux_name'] + ' Enter' in log

        writable_b, csrf_b = login(port, 'write-secret')
        status, body, _ = request(
            port,
            f'/mobile-input/{task_id}',
            method='POST',
            body={'text': 'occupied', 'enter': True},
            cookie=writable_b,
            csrf=csrf_b,
            host=f'127.0.0.1:{port}',
            origin=f'http://127.0.0.1:{port}',
            allow_error=True,
        )
        assert status == 409
        assert json.loads(body)['code'] == 'write_lease_held'
    finally:
        core.codex_web_gateway_stop()


def test_ttyd_launches_with_mobile_readable_font_size(env):
    core = load_core()
    source = Path(core.__file__).read_text(encoding='utf-8')
    assert '"--client-option", "fontSize=12"' not in source
    assert 'fontSize:(window.innerWidth<=760?6:13)' in source


def test_provider_spawn_disable_short_circuits_native_launchers(env, monkeypatch):
    core = load_core()
    task = type('Task', (), {'id': 't_disabled', 'workspace_path': str(env)})()
    monkeypatch.setenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', '1')
    monkeypatch.setattr(core.shutil, 'which', lambda name: (_ for _ in ()).throw(AssertionError('launcher should not inspect binaries')))

    assert core._provider_spawn_disabled() is True
    assert core.codex_native_run_task('board', task, {'workdir': str(env)})['error'].startswith('provider spawn disabled')
    assert core.claude_interactive_run_task('board', task, {'workdir': str(env)})['provider'] == 'claude'
    assert core.hermes_native_run_task('board', task, {'workdir': str(env)})['provider'] == 'hermes'
    assert core.codex_native_init_role_session('board', task, {'workdir': str(env)})['provider'] == 'codex'


def test_provider_spawn_disable_still_reuses_existing_native_sessions(env, monkeypatch):
    core = load_core()
    task = type('Task', (), {'id': 't_reuse', 'workspace_path': str(env)})()
    monkeypatch.setenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', '1')
    monkeypatch.setattr(core.shutil, 'which', lambda name: (_ for _ in ()).throw(AssertionError('reused session should not inspect binaries')))
    monkeypatch.setattr(core, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    monkeypatch.setattr(core, '_url_ok', lambda url: True)

    core._write_json_file(core._codex_web_state_path(task.id), {'pid': 111, 'tmux_name': 'tmux-codex', 'url': 'http://127.0.0.1:1/'})
    core._write_json_file(core._claude_web_state_path(task.id), {'pid': 222, 'tmux_name': 'tmux-claude', 'url': 'http://127.0.0.1:2/'})
    core._write_json_file(core._hermes_web_state_path(task.id), {'pid': 333, 'tmux_name': 'tmux-hermes', 'url': 'http://127.0.0.1:3/'})

    assert core.codex_native_run_task('board', task, {'workdir': str(env)})['reused'] is True
    assert core.codex_native_init_role_session('board', task, {'workdir': str(env)})['reused'] is True
    assert core.claude_interactive_run_task('board', task, {'workdir': str(env)})['reused'] is True
    assert core.hermes_native_run_task('board', task, {'workdir': str(env)})['reused'] is True


def test_remote_ttyd_launch_commands_include_backend_credentials(env, monkeypatch):
    core = load_core()
    task = type('Task', (), {
        'id': 't_guarded_launch',
        'title': 'Guarded launch',
        'body': 'body',
        'workspace_path': str(env),
    })()
    popens = []
    monkeypatch.setenv('KANBAN_AGENCY_TTYD_BACKEND_CREDENTIALS', '1')
    monkeypatch.delenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', raising=False)
    monkeypatch.setattr(core.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(core, '_pid_alive', lambda pid: False)
    monkeypatch.setattr(core, '_url_ok', lambda url: False)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    monkeypatch.setattr(core, '_free_port', lambda: 41001 + len(popens))
    monkeypatch.setattr(core.subprocess, 'run', lambda *args, **kwargs: type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})())
    monkeypatch.setattr(core, '_latest_codex_thread_for_cwd', lambda cwd, started_at: 'thread-guarded')
    monkeypatch.setattr(core, '_mark_running', lambda conn, task_id: None)
    monkeypatch.setattr(core, 'ensure_codex_session_link', lambda *args, **kwargs: None)
    monkeypatch.setattr(core.kb, 'add_comment', lambda *args, **kwargs: None)

    class FakeConn:
        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(core.kb, 'connect', lambda board=None: FakeConn())

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 5000 + len(popens)
            popens.append(cmd)

    monkeypatch.setattr(core.subprocess, 'Popen', FakePopen)
    out = core.codex_native_init_role_session('guarded_launch', task, {'workdir': str(env), 'provider': 'codex'})
    assert out['ok'] is True
    assert len(popens) == 2
    assert all('--credential' in cmd for cmd in popens)
    state = core._read_json_file(core._codex_web_state_path(task.id))
    assert state['backend_credential']
    assert state['readonly_backend_credential']
    assert state['backend_credential'] != state['readonly_backend_credential']


def test_claude_remote_ttyd_launch_commands_include_backend_credentials(env, monkeypatch):
    core = load_core()
    task = type('Task', (), {
        'id': 't_claude_guarded_launch',
        'title': 'Claude guarded launch',
        'body': 'body',
        'workspace_path': str(env),
    })()
    popens = []
    monkeypatch.setenv('KANBAN_AGENCY_TTYD_BACKEND_CREDENTIALS', '1')
    monkeypatch.delenv('KANBAN_AGENCY_DISABLE_PROVIDER_SPAWN', raising=False)
    monkeypatch.setattr(core.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(core, '_pid_alive', lambda pid: False)
    monkeypatch.setattr(core, '_url_ok', lambda url: False)
    monkeypatch.setattr(core, '_tmux_has_session', lambda name: True)
    ports = iter([42001, 42002])
    monkeypatch.setattr(core, '_free_port', lambda: next(ports))
    monkeypatch.setattr(core.subprocess, 'run', lambda *args, **kwargs: type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})())
    monkeypatch.setattr(core, '_mark_running', lambda conn, task_id: None)
    monkeypatch.setattr(core, '_set_status', lambda *args, **kwargs: None)
    monkeypatch.setattr(core, 'ensure_claude_session_link', lambda *args, **kwargs: None)
    monkeypatch.setattr(core.kb, 'add_comment', lambda *args, **kwargs: None)

    class FakeConn:
        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(core.kb, 'connect', lambda board=None: FakeConn())

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 6000 + len(popens)
            popens.append(cmd)

    monkeypatch.setattr(core.subprocess, 'Popen', FakePopen)
    out = core.claude_interactive_run_task('guarded_claude', task, {'workdir': str(env), 'provider': 'claude'})
    assert out['ok'] is True
    assert len(popens) == 2
    assert all('--credential' in cmd for cmd in popens)
    state = core._read_json_file(core._claude_web_state_path(task.id))
    assert state['readonly_url'] == 'http://127.0.0.1:42002/'
    assert state['backend_credential']
    assert state['readonly_backend_credential']
    assert state['backend_credential'] != state['readonly_backend_credential']


def test_remote_ttyd_missing_session_state_is_not_bad_gateway(env):
    core = load_core()
    port = free_port()
    task_id = make_task(core, 'remote_missing_ttyd_state', str(env))
    core._codex_web_state_path(task_id).unlink()
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, f'/ttyd/{task_id}?mode=auto', cookie=readonly, host=f'127.0.0.1:{port}', allow_error=True)
        assert status == 404
        data = json.loads(body)
        assert data['code'] == 'session_state_missing'
    finally:
        core.codex_web_gateway_stop()


def test_remote_readonly_does_not_fallback_to_writable_ttyd(env):
    core = load_core()
    port = free_port()
    backend, backend_url = start_backend('writable only ttyd')
    task_id = make_task(core, 'remote_readonly_no_fallback', str(env), url=backend_url, readonly_url='')
    state_path = core._codex_web_state_path(task_id)
    state = core._read_json_file(state_path)
    state.pop('readonly_url', None)
    state.pop('readonly_pid', None)
    state['backend_credential'] = 'gateway-write:secret'
    core._write_json_file(state_path, state)
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, f'/ttyd/{task_id}?mode=auto', cookie=readonly, host=f'127.0.0.1:{port}', allow_error=True)
        assert status == 404
        assert json.loads(body)['code'] == 'readonly_ttyd_not_found'

        writable, csrf = login(port, 'write-secret')
        status, body, _ = request(port, f'/ttyd/{task_id}?mode=write', cookie=writable, csrf=csrf, host=f'127.0.0.1:{port}')
        assert status == 200
        assert 'writable only ttyd' in body
    finally:
        backend.shutdown()
        core.codex_web_gateway_stop()


def test_remote_ttyd_uses_hermes_web_state(env):
    core = load_core()
    port = free_port()
    backend, backend_url = start_backend('hermes ttyd')
    task_id = make_task(core, 'remote_hermes_ttyd', str(env))
    core._codex_web_state_path(task_id).unlink()
    core._write_json_file(core._hermes_web_state_path(task_id), {
        'task_id': task_id,
        'board': 'remote_hermes_ttyd',
        'provider': 'hermes',
        'url': backend_url,
        'readonly_url': backend_url,
        'pid': 1234,
        'readonly_pid': 1235,
        'tmux_name': 'kanban-hermes-' + task_id,
        'backend_credential': 'gateway-write:secret',
        'readonly_backend_credential': 'gateway-read:secret',
    })
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, f'/ttyd/{task_id}?mode=auto', cookie=readonly, host=f'127.0.0.1:{port}')
        assert status == 200
        assert 'hermes ttyd' in body
    finally:
        backend.shutdown()
        core.codex_web_gateway_stop()


def test_remote_sessions_preserve_attention_and_filter_archived_roles(env):
    core = load_core()
    port = free_port()
    task_id = make_task(core, 'remote_attention', str(env))
    archived_id = make_task(core, 'remote_archived_filter', str(env))
    conn = core.kb.connect(board='remote_attention')
    try:
        with core.kb.write_txn(conn):
            core._set_status(conn, task_id, 'blocked', result='Needs user approval')
    finally:
        conn.close()
    conn = core.kb.connect(board='remote_archived_filter')
    try:
        with core.kb.write_txn(conn):
            core._set_status(conn, archived_id, 'archived')
    finally:
        conn.close()

    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, '/sessions', cookie=readonly, host=f'127.0.0.1:{port}')
        assert status == 200
        data = json.loads(body)
        serialized = json.dumps(data)
        assert archived_id not in serialized
        role = next(r for root in data['roots'] for r in root['roles'] if r.get('task_id') == task_id)
        root = next(root for root in data['roots'] if any(r.get('task_id') == task_id for r in root['roles']))
        assert role['pending_approval'] is True
        assert role['pending']['kind'] == 'blocked'
        assert root['attention'] == 1
        assert 'changed_at' in root
        assert 'changed_at' in role
        assert data['completed_session_cleanup']['skipped'] == 'remote-read'
    finally:
        core.codex_web_gateway_stop()


def test_remote_sessions_do_not_auto_advance_ready_roles(env):
    core = load_core()
    port = free_port()
    task_id = make_task(core, 'remote_no_auto_advance', str(env))
    conn = core.kb.connect(board='remote_no_auto_advance')
    try:
        with core.kb.write_txn(conn):
            core._set_status(conn, task_id, 'ready')
    finally:
        conn.close()

    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        readonly, _ = login(port, 'read-secret')
        status, body, _ = request(port, '/sessions', cookie=readonly, host=f'127.0.0.1:{port}')
        assert status == 200
        data = json.loads(body)
        role = next(r for root in data['roots'] for r in root['roles'] if r.get('task_id') == task_id)
        assert role['task_status'] == 'ready'
        assert data['completed_session_cleanup']['skipped'] == 'remote-read'
        conn = core.kb.connect(board='remote_no_auto_advance')
        try:
            row = conn.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
            assert row['status'] == 'ready'
        finally:
            conn.close()
    finally:
        core.codex_web_gateway_stop()


def test_remote_form_login_and_ttyd_websocket_tunnel(env):
    core = load_core()
    port = free_port()
    websocket_url, seen, done = start_websocket_backend()
    task_id = make_task(core, 'remote_ws', str(env), url=websocket_url, readonly_url=websocket_url)
    add_backend_credentials(core, task_id)
    out = core.codex_web_gateway_start(host='127.0.0.1', remote=True, auth_file=str(auth_file(env, port)), port=port)
    assert out['ok'] is True
    try:
        wait_ready(port)
        status, body, headers = request_form(
            port,
            '/login',
            {'secret': 'write-secret'},
            host=f'127.0.0.1:{port}',
            origin=f'http://127.0.0.1:{port}',
        )
        assert status == 200
        login_data = json.loads(body)
        assert login_data['role'] == 'writable'
        cookie = headers['Set-Cookie'].split(';', 1)[0]

        with socket.create_connection(('127.0.0.1', port), timeout=5) as client:
            client.sendall(
                (
                    f'GET /ttyd/{task_id}/ws?mode=write HTTP/1.1\r\n'
                    f'Host: 127.0.0.1:{port}\r\n'
                    f'Origin: http://127.0.0.1:{port}\r\n'
                    f'Cookie: {cookie}\r\n'
                    f'X-CSRF-Token: {login_data["csrf_token"]}\r\n'
                    'Upgrade: websocket\r\n'
                    'Connection: Upgrade\r\n'
                    'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
                    'Sec-WebSocket-Version: 13\r\n'
                    '\r\n'
                ).encode('iso-8859-1')
            )
            response = client.recv(4096).decode('iso-8859-1', errors='replace')
        assert '101 Switching Protocols' in response
        assert done.wait(2)
        assert 'GET /ws?mode=write HTTP/1.1' in seen['request']
        assert 'Cookie:' not in seen['request']
        assert 'X-CSRF-Token:' not in seen['request']
    finally:
        core.codex_web_gateway_stop()


def test_remote_gateway_works_through_local_reverse_proxy_entrypoint(env):
    core = load_core()
    gateway_port = free_port()
    backend, backend_url = start_backend('<html><head></head><body>proxied ttyd</body></html>')
    websocket_url, seen, done = start_websocket_backend()
    task_id = make_task(core, 'remote_proxy_smoke', str(env), url=websocket_url, readonly_url=backend_url)
    add_backend_credentials(core, task_id)
    proxy = None
    proxy_port = free_port()
    public_host = f'remote.example.test:{proxy_port}'
    public_origin = f'http://{public_host}'
    out = core.codex_web_gateway_start(
        host='127.0.0.1',
        remote=True,
        auth_file=str(auth_file_for_entrypoint(env, public_host, public_origin)),
        port=gateway_port,
    )
    assert out['ok'] is True
    try:
        wait_ready(gateway_port)
        proxy, actual_proxy_port = start_reverse_proxy(gateway_port, listen_port=proxy_port)
        assert actual_proxy_port == proxy_port

        status, body, headers = request(
            proxy_port,
            '/login',
            method='POST',
            body={'secret': 'read-secret'},
            host=public_host,
            origin=public_origin,
        )
        assert status == 200
        readonly_cookie = headers['Set-Cookie'].split(';', 1)[0]

        status, body, headers = request(
            proxy_port,
            '/login',
            method='POST',
            body={'secret': 'write-secret'},
            host=public_host,
            origin=public_origin,
        )
        assert status == 200
        login_data = json.loads(body)
        assert login_data['role'] == 'writable'
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        csrf = login_data['csrf_token']

        status, body, _ = request(proxy_port, '/cockpit', cookie=cookie, host=public_host)
        assert status == 200
        assert 'Session Cockpit' in body

        status, body, _ = request(proxy_port, '/sessions', cookie=cookie, host=public_host)
        assert status == 200
        assert task_id in body

        status, body, _ = request(proxy_port, f'/ttyd/{task_id}', cookie=readonly_cookie, host=public_host)
        assert status == 200
        assert 'proxied ttyd' in body

        status, body, _ = request(
            proxy_port,
            f'/resume/{task_id}',
            method='POST',
            body={},
            cookie=cookie,
            csrf=csrf,
            host=public_host,
            origin=public_origin,
            allow_error=True,
        )
        assert status in (200, 400)
        assert 'ok' in json.loads(body)

        with socket.create_connection(('127.0.0.1', proxy_port), timeout=5) as client:
            client.sendall(
                (
                    f'GET /ttyd/{task_id}/ws?mode=write&csrf={csrf} HTTP/1.1\r\n'
                    f'Host: {public_host}\r\n'
                    f'Origin: {public_origin}\r\n'
                    f'Cookie: {cookie}\r\n'
                    'Upgrade: websocket\r\n'
                    'Connection: Upgrade\r\n'
                    'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
                    'Sec-WebSocket-Version: 13\r\n'
                    '\r\n'
                ).encode('iso-8859-1')
            )
            response = client.recv(4096).decode('iso-8859-1', errors='replace')
        assert '101 Switching Protocols' in response
        assert done.wait(2)
        assert 'GET /ws?mode=write HTTP/1.1' in seen['request']
    finally:
        if proxy:
            proxy.shutdown()
        backend.shutdown()
        core.codex_web_gateway_stop()
