# Remote Control Deployment

Kanban Agency remote control extends the existing `codex-web-gateway`. It does not add a business Relay. Authentication, authorization, CSRF checks, write leases, task/session lookup, and ttyd backend selection all stay in the gateway.

## No built-in HTTPS/TLS

Remote mode has no built-in HTTPS/TLS termination. Public access must be protected by SSH tunnel, VPN, trusted private networking, or a reverse proxy that provides TLS or equivalent transport protection.

## Topology A: Kanban Agency directly on ECS

Use this when the workspace, Hermes Kanban state, tmux sessions, Codex/Claude sessions, and `codex-web-gateway` all run on the ECS host.

```bash
scripts/kanban-agency codex-web-gateway \
  --remote \
  --host 127.0.0.1 \
  --port <gateway_port> \
  --auth-file <remote-auth.json>
```

Expose it through Nginx/Caddy on the same ECS host, or bind `--host 0.0.0.0` only inside a trusted network and with remote auth configured. Direct public HTTP is only suitable for a short controlled smoke window.

## Topology B: ECS reverse proxy to gateway

Use this when Kanban Agency runs on a host reachable from ECS over private networking or VPN.

```text
Browser -> ECS public IP/domain -> Nginx/Caddy/TLS -> gateway private address
```

The reverse proxy must forward both HTTP and WebSocket traffic. It must preserve the browser-facing `Host` and `Origin` values expected by the gateway auth file, because remote mode validates them before allowing writes.

Minimum Nginx shape:

```nginx
location / {
    proxy_pass http://<gateway_private_host>:<gateway_port>;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Origin $http_origin;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_read_timeout 3600s;
}
```

The same rules apply to `/ttyd/<task_id>` and `/ttyd/<task_id>/ws`: WebSocket upgrade headers must reach the gateway, not a raw ttyd backend.

## Topology C: ECS as forwarding entry

Use this when Kanban Agency stays on a local workstation and the workstation cannot accept public inbound traffic.

Run the gateway locally:

```bash
scripts/kanban-agency codex-web-gateway \
  --remote \
  --host 127.0.0.1 \
  --port <gateway_port> \
  --auth-file <remote-auth.json>
```

Option 1 is the built-in dumb forwarding relay. Run this on ECS:

```bash
scripts/kanban-agency relay-server \
  --public-host 127.0.0.1 \
  --public-port <ecs_public_backend_port> \
  --agent-host 0.0.0.0 \
  --agent-port <ecs_agent_port> \
  --token <short_lived_relay_token>
```

Run this on the workstation:

```bash
scripts/kanban-agency relay-client \
  --relay-host <ecs_host> \
  --relay-port <ecs_agent_port> \
  --target-host 127.0.0.1 \
  --target-port <gateway_port> \
  --connections 8 \
  --token <short_lived_relay_token>
```

Point Nginx/Caddy on ECS to `127.0.0.1:<ecs_public_backend_port>`.

Option 2 is a plain SSH reverse tunnel:

```bash
ssh -N -R <ecs_tunnel_port>:127.0.0.1:<gateway_port> <ecs>
```

On ECS, point Nginx/Caddy to `127.0.0.1:<ecs_tunnel_port>`.

The built-in relay and the SSH reverse tunnel are not a business Relay. The ECS host does not store task/session state, does not select ttyd backends, does not participate in authz/write lease decisions, and does not understand Kanban task semantics. It only forwards HTTP and WebSocket bytes to `codex-web-gateway`.

## Auth File

Remote auth should include the browser-facing entrypoint, not the raw backend address:

```json
{
  "readonly_secret": "<readonly secret>",
  "writable_secret": "<writable secret>",
  "allowed_hosts": ["remote.example.com"],
  "allowed_origins": ["https://remote.example.com"]
}
```

If testing with an IP and port, include the port:

```json
{
  "readonly_secret": "r",
  "writable_secret": "w",
  "allowed_hosts": ["10.0.0.10:8766"],
  "allowed_origins": ["http://10.0.0.10:8766"]
}
```

State-changing requests require writable login and CSRF. The Cockpit and mobile pages obtain the CSRF token from `/auth/me` and send it in `X-CSRF-Token`.

Mobile users can use `/mobile` for a single-terminal shell. Its fallback input form posts to `/mobile-input/<task_id>`; the gateway accepts it only for writable sessions with valid CSRF, allowed `Origin`, and an active write lease, then sends the text and Enter to the recorded tmux session. The fallback does not bypass raw ttyd credentials and does not send input directly to raw ttyd.

## Local smoke

Run the local reverse proxy smoke from the test suite:

```bash
python -m pytest tests/test_remote_control_gateway.py::test_remote_gateway_works_through_local_reverse_proxy_entrypoint -q
python -m pytest tests/test_relay_transport.py -q
```

That smoke uses two local ports to simulate an ECS/public entrypoint in front of the gateway. It verifies:

- login through the proxy with the public `Host` and `Origin`;
- `/cockpit` and `/sessions` through the proxy;
- `/ttyd/<task_id>` through the gateway path;
- `/ttyd/<task_id>/ws` WebSocket upgrade through the proxy;
- writable POST with CSRF through the proxy.

The relay smoke verifies that `relay-server` and `relay-client` preserve the public `Host`, forward POST bodies, and pass `/ttyd/<task_id>` WebSocket upgrade bytes without storing Kanban state.

For a real ECS smoke, provide the ECS login method or run the commands yourself, the gateway directory and start command, the chosen topology, public domain/IP and port, TLS/reverse proxy method, and short-lived readonly/writable test credentials.
