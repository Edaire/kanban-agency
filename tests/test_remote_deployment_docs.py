from pathlib import Path


def test_remote_control_deployment_doc_covers_public_access_topologies():
    doc = Path(__file__).resolve().parents[1] / 'docs' / 'remote-control-deployment.md'
    text = doc.read_text(encoding='utf-8')

    required = [
        'Topology A',
        'Kanban Agency directly on ECS',
        'Topology B',
        'ECS reverse proxy',
        'Topology C',
        'SSH reverse tunnel',
        'ssh -N -R <ecs_tunnel_port>:127.0.0.1:<gateway_port> <ecs>',
        'relay-server',
        'relay-client',
        '--agent-host 0.0.0.0',
        '--agent-port',
        '--target-port <gateway_port>',
        'No built-in HTTPS/TLS',
        'WebSocket',
        '/ttyd/<task_id>',
        'not a business Relay',
        'does not store task/session state',
        'CSRF',
        'Host',
        'Origin',
        'Local smoke',
    ]
    missing = [item for item in required if item not in text]
    assert missing == []
