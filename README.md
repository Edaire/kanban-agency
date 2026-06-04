# kanban-agency

A Hermes plugin for role-oriented Kanban workflows with native tmux-backed Codex/Claude sessions and a browser cockpit.

## Features

- Role workflow cards for analyst, architect, developer, tester, assistant, and ops.
- Native tmux execution surface for Codex roles.
- Browser cockpit for multiple boards/sessions with fixed pane IDs.
- Writable ttyd panes with tmux-backed scroll handling.
- Session lifecycle/status syncing and gateway endpoints.

## Requirements

- Hermes Agent with `hermes_cli.kanban_db` available.
- Python 3.11+.
- `tmux` and `ttyd` for browser terminal sessions.
- Codex CLI for `provider: codex`; Claude CLI for `provider: claude` if used.

## Install locally

```bash
mkdir -p ~/.hermes/plugins
cp -R . ~/.hermes/plugins/kanban-agency
mkdir -p ~/.hermes/kanban-agency
cp examples/roles.yaml ~/.hermes/kanban-agency/roles.yaml
```

Adjust `~/.hermes/kanban-agency/roles.yaml` rule paths for your projects.

## CLI

```bash
scripts/kanban-agency scan --board my_board
scripts/kanban-agency start --board my_board
scripts/kanban-agency run --board my_board --task-id t_xxx
scripts/kanban-agency codex-web-gateway --port 8766
```

Open:

```text
http://127.0.0.1:8766/cockpit
```

## Tests

```bash
python -m pytest tests -q
```

## Notes

This repository intentionally excludes local board databases, session logs, credentials, and organization-specific role rules.
