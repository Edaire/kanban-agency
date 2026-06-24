import importlib.util
import builtins
import http.client
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def load_core():
    path = Path(__file__).resolve().parents[1] / 'core.py'
    spec = importlib.util.spec_from_file_location('ka_core_relay_transport_under_test', path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_relay_module_imports_without_hermes_cli(monkeypatch):
    original_import = builtins.__import__

    def block_hermes(name, *args, **kwargs):
        if name == 'hermes_cli' or name.startswith('hermes_cli.'):
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', block_hermes)
    core = load_core()
    assert core.kb is None
    assert callable(core.relay_server_start)


def test_relay_module_imports_from_shallow_install_without_hermes_cli(monkeypatch, tmp_path):
    source = Path(__file__).resolve().parents[1] / 'core.py'
    installed = tmp_path / 'core.py'
    installed.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
    original_import = builtins.__import__

    def block_hermes(name, *args, **kwargs):
        if name == 'hermes_cli' or name.startswith('hermes_cli.'):
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', block_hermes)
    spec = importlib.util.spec_from_file_location('ka_core_shallow_relay_import_under_test', installed)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    assert mod.kb is None
    assert callable(mod.relay_server_start)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def start_backend():
    seen = []

    class H(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            seen.append({'method': 'GET', 'path': self.path, 'host': self.headers.get('Host')})
            body = f'backend:{self.path}'.encode()
            self.send_response(200)
            self.send_header('content-type', 'text/plain')
            self.send_header('content-length', str(len(body)))
            self.send_header('connection', 'close')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length') or '0'))
            seen.append({'method': 'POST', 'path': self.path, 'body': body.decode(), 'host': self.headers.get('Host')})
            out = b'echo:' + body
            self.send_response(200)
            self.send_header('content-type', 'text/plain')
            self.send_header('content-length', str(len(out)))
            self.send_header('connection', 'close')
            self.end_headers()
            self.wfile.write(out)

    server = ThreadingHTTPServer(('127.0.0.1', 0), H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1], seen


def wait_for(fn, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return
        time.sleep(0.05)
    raise AssertionError('condition not met')


def request(port, method, path, body=None):
    headers = {'Host': 'public.example.test', 'Connection': 'close'}
    if body is not None:
        body = body.encode()
        headers['Content-Type'] = 'text/plain'
        headers['Content-Length'] = str(len(body))
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode()
        return resp.status, text
    finally:
        conn.close()


def test_relay_server_client_forward_http_without_business_state():
    core = load_core()
    backend, backend_port, seen = start_backend()
    public_port = free_port()
    agent_port = free_port()
    server = core.relay_server_start(public_host='127.0.0.1', public_port=public_port, agent_host='127.0.0.1', agent_port=agent_port, token='relay-token')
    client = core.relay_client_start(relay_host='127.0.0.1', relay_port=agent_port, token='relay-token', target_host='127.0.0.1', target_port=backend_port, connections=2)
    try:
        wait_for(lambda: len(server.agents) >= 1)
        status, text = request(public_port, 'GET', '/sessions?x=1')
        assert status == 200
        assert text == 'backend:/sessions?x=1'

        status, text = request(public_port, 'POST', '/complete/t_1', 'done')
        assert status == 200
        assert text == 'echo:done'

        assert [x['method'] for x in seen] == ['GET', 'POST']
        assert all(x['host'] == 'public.example.test' for x in seen)
        assert not hasattr(server, 'kanban_state')
    finally:
        client.stop()
        server.stop()
        backend.shutdown()


def test_relay_forwards_websocket_upgrade_bytes():
    core = load_core()
    public_port = free_port()
    agent_port = free_port()
    backend_ready = threading.Event()
    seen = {}
    backend_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    backend_sock.bind(('127.0.0.1', 0))
    backend_sock.listen(1)
    backend_port = backend_sock.getsockname()[1]

    def backend():
        backend_ready.set()
        conn, _ = backend_sock.accept()
        with conn:
            data = conn.recv(4096)
            seen['request'] = data.decode('iso-8859-1', errors='replace')
            conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
        backend_sock.close()

    threading.Thread(target=backend, daemon=True).start()
    backend_ready.wait(2)
    server = core.relay_server_start(public_host='127.0.0.1', public_port=public_port, agent_host='127.0.0.1', agent_port=agent_port, token='relay-token')
    client = core.relay_client_start(relay_host='127.0.0.1', relay_port=agent_port, token='relay-token', target_host='127.0.0.1', target_port=backend_port, connections=1)
    try:
        wait_for(lambda: len(server.agents) >= 1)
        with socket.create_connection(('127.0.0.1', public_port), timeout=5) as s:
            s.sendall(
                b'GET /ttyd/t_1/ws?mode=auto HTTP/1.1\r\n'
                b'Host: public.example.test\r\n'
                b'Upgrade: websocket\r\n'
                b'Connection: Upgrade\r\n'
                b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n'
                b'Sec-WebSocket-Version: 13\r\n'
                b'\r\n'
            )
            response = s.recv(4096).decode('iso-8859-1', errors='replace')
        assert '101 Switching Protocols' in response
        assert 'GET /ttyd/t_1/ws?mode=auto HTTP/1.1' in seen['request']
        assert 'Host: public.example.test' in seen['request']
    finally:
        client.stop()
        server.stop()


def test_relay_streams_headers_before_request_body_for_expect_continue():
    core = load_core()
    public_port = free_port()
    agent_port = free_port()
    backend_ready = threading.Event()
    backend_done = threading.Event()
    seen = {}
    backend_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    backend_sock.bind(('127.0.0.1', 0))
    backend_sock.listen(1)
    backend_port = backend_sock.getsockname()[1]

    def backend():
        backend_ready.set()
        conn, _ = backend_sock.accept()
        with conn:
            data = b''
            while b'\r\n\r\n' not in data:
                data += conn.recv(4096)
            conn.sendall(b'HTTP/1.1 100 Continue\r\n\r\n')
            while b'hello' not in data:
                data += conn.recv(4096)
            seen['request'] = data.decode('iso-8859-1', errors='replace')
            conn.sendall(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\nok')
        backend_sock.close()
        backend_done.set()

    threading.Thread(target=backend, daemon=True).start()
    backend_ready.wait(2)
    server = core.relay_server_start(public_host='127.0.0.1', public_port=public_port, agent_host='127.0.0.1', agent_port=agent_port, token='relay-token')
    client = core.relay_client_start(relay_host='127.0.0.1', relay_port=agent_port, token='relay-token', target_host='127.0.0.1', target_port=backend_port, connections=1)
    try:
        wait_for(lambda: len(server.agents) >= 1)
        with socket.create_connection(('127.0.0.1', public_port), timeout=5) as s:
            s.settimeout(2)
            s.sendall(
                b'POST /upload HTTP/1.1\r\n'
                b'Host: public.example.test\r\n'
                b'Expect: 100-continue\r\n'
                b'Content-Length: 5\r\n'
                b'\r\n'
            )
            assert b'100 Continue' in s.recv(4096)
            s.sendall(b'hello')
            response = b''
            while b'200 OK' not in response:
                response += s.recv(4096)
        assert b'ok' in response
        assert 'POST /upload HTTP/1.1' in seen['request']
        assert backend_done.wait(2)
    finally:
        client.stop()
        server.stop()


def test_relay_preserves_split_content_length_request_body():
    core = load_core()
    public_port = free_port()
    agent_port = free_port()
    backend_ready = threading.Event()
    seen = {}
    backend_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    backend_sock.bind(('127.0.0.1', 0))
    backend_sock.listen(1)
    backend_port = backend_sock.getsockname()[1]

    def backend():
        backend_ready.set()
        conn, _ = backend_sock.accept()
        with conn:
            data = b''
            while b'\r\n\r\n' not in data:
                data += conn.recv(4096)
            headers, _, body = data.partition(b'\r\n\r\n')
            length = 11
            while len(body) < length:
                body += conn.recv(4096)
            seen['request'] = headers.decode('iso-8859-1', errors='replace')
            seen['body'] = body[:length]
            conn.sendall(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 2\r\n\r\nok')
        backend_sock.close()

    threading.Thread(target=backend, daemon=True).start()
    backend_ready.wait(2)
    server = core.relay_server_start(public_host='127.0.0.1', public_port=public_port, agent_host='127.0.0.1', agent_port=agent_port, token='relay-token')
    client = core.relay_client_start(relay_host='127.0.0.1', relay_port=agent_port, token='relay-token', target_host='127.0.0.1', target_port=backend_port, connections=1)
    try:
        wait_for(lambda: len(server.agents) >= 1)
        with socket.create_connection(('127.0.0.1', public_port), timeout=5) as s:
            s.settimeout(2)
            s.sendall(
                b'POST /login HTTP/1.1\r\n'
                b'Host: public.example.test\r\n'
                b'Content-Length: 11\r\n'
                b'\r\n'
            )
            time.sleep(0.1)
            s.sendall(b'hello world')
            response = b''
            while b'200 OK' not in response:
                response += s.recv(4096)
        assert b'ok' in response
        assert 'POST /login HTTP/1.1' in seen['request']
        assert seen['body'] == b'hello world'
    finally:
        client.stop()
        server.stop()


def test_relay_skips_stale_agent_socket_when_fresh_agent_available():
    core = load_core()
    backend, backend_port, _seen = start_backend()
    public_port = free_port()
    agent_port = free_port()
    server = core.relay_server_start(public_host='127.0.0.1', public_port=public_port, agent_host='127.0.0.1', agent_port=agent_port, token='relay-token')
    client = core.relay_client_start(relay_host='127.0.0.1', relay_port=agent_port, token='relay-token', target_host='127.0.0.1', target_port=backend_port, connections=1)
    stale_a, stale_b = socket.socketpair()
    stale_b.close()
    try:
        with server._lock:
            server._agents.insert(0, stale_a)
        wait_for(lambda: len(server.agents) >= 2)
        status, text = request(public_port, 'GET', '/sessions?after=stale')
        assert status == 200
        assert text == 'backend:/sessions?after=stale'
    finally:
        client.stop()
        server.stop()
        backend.shutdown()
        stale_a.close()
