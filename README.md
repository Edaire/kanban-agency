# kanban-agency

A Hermes plugin for role-oriented Kanban workflows with native tmux-backed Codex/Claude sessions and a browser cockpit.

## What it does

kanban-agency turns a Hermes Kanban board into role/session workflows:

- role lanes such as analyst, architect, developer, tester, assistant, ops
- native tmux sessions for Codex/Claude roles
- a browser cockpit for multiple live role sessions
- writable ttyd panes with tmux-backed scroll handling
- workflow monitoring, blocked/running status sync, and session resume routes

## Requirements

- Hermes Agent with `hermes_cli.kanban_db` available
- Python 3.11+
- `tmux`
- `ttyd`
- Codex CLI for `provider: codex`
- Claude CLI for `provider: claude` if you use Claude roles

On macOS:

```bash
brew install tmux ttyd
```

## Install as a local Hermes plugin

```bash
mkdir -p ~/.hermes/plugins
cp -R . ~/.hermes/plugins/kanban-agency
```

Create private role config and rule files:

```bash
mkdir -p ~/.hermes/kanban-agency/rules
cp templates/roles.yaml.template ~/.hermes/kanban-agency/roles.yaml
for f in templates/rules/*.template; do
  name="$(basename "$f" .template)"
  cp "$f" "$HOME/.hermes/kanban-agency/rules/$name"
done
```

Edit:

```text
~/.hermes/kanban-agency/roles.yaml
~/.hermes/kanban-agency/rules/*.md
```

Keep these files private. They usually contain project-specific rules, paths, and internal conventions.

## Basic workflow

Create or pick a Hermes Kanban board, then scan/start/run tasks with the plugin CLI:

```bash
scripts/kanban-agency scan --board my_board
scripts/kanban-agency start --board my_board
scripts/kanban-agency run --board my_board --task-id t_xxx
```

Task bodies can include an absolute workdir:

```text
workdir: /absolute/path/to/project
```

For Codex roles, `run` starts a tmux session and opens Codex inside it.

## Browser cockpit

Start the gateway:

```bash
scripts/kanban-agency codex-web-gateway --port 8766
```

Open:

```text
http://127.0.0.1:8766/cockpit
```

Useful routes:

```text
/cockpit                    multi-session cockpit
/s/<task_id>                writable single-session page
/status/<task_id>           session/task status JSON
/sessions                   all session summaries
/sessions/<board>           board-scoped session summaries
/resume/<task_id>           resume/rebuild stopped session
/tmux-scroll/<task_id>      tmux copy-mode scroll endpoint used by ttyd wheel handler
```

## Local/private files

Do not commit runtime state or private role config. `.gitignore` excludes common local files:

```text
roles.yaml
rules/
codex-web/
*.db
*.sqlite
*.jsonl
*.pid
*.stdout.log
*.stderr.log
.hermes/
```

Use `templates/` and `examples/` for shareable defaults.

## Tests

When testing this standalone repository, make Hermes Agent importable:

```bash
PYTHONPATH=/path/to/hermes-agent python -m pytest tests -q
```

Current local verification:

```text
40 passed
```

## Security / open-source checklist

Before pushing publicly:

```bash
git status --short
python -m pytest tests -q
python - <<'PY'
from pathlib import Path
patterns=['token','password','secret','authorization','10.','internal','corp']
for p in Path('.').rglob('*'):
    if p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts:
        text=p.read_text(errors='ignore')
        for pat in patterns:
            if pat.lower() in text.lower():
                print(p, pat)
PY
```

Some third-party bundled ttyd/xterm code may contain generic words like `password` in minified source. Review findings manually.

## License

MIT
