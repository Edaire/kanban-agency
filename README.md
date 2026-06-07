# Kanban Agency

**Kanban-native multi-agent orchestration for Hermes Agent, OpenAI Codex CLI, and Anthropic Claude Code.**

Kanban Agency turns a Hermes Kanban board into a live AI-agent cockpit: split a feature into roles, run each role in its own native Codex or Claude Code terminal, and keep the whole workflow auditable through Kanban tasks.

> Hermes Agent provides the Kanban/task layer. Codex and Claude Code provide native agent sessions. Kanban Agency wires them together with tmux, ttyd, and a browser cockpit.

## Why this exists

Most AI coding workflows are either:

- one giant chat thread that is hard to audit,
- a pile of terminal sessions with no workflow state, or
- a custom UI that hides the native Codex / Claude Code experience.

Kanban Agency keeps the native tools intact while adding workflow structure:

```text
Hermes Kanban task
  -> role card: analyst / architect / developer / tester / ops
  -> native Codex CLI or Claude Code session in tmux
  -> browser Cockpit pane through ttyd
  -> Kanban status, comments, dependencies, and human Complete gate
```

## Highlights

- **Hermes-native Kanban workflow** — tasks, comments, dependencies, and audit history stay in Hermes Kanban.
- **Role-based multi-agent flow** — precreate analyst -> architect -> developer -> tester lanes for feature work.
- **OpenAI Codex CLI support** — run Codex as a persistent native TUI session, not a fake wrapper.
- **Anthropic Claude Code support** — run Claude Code for ops / review / interactive workflows.
- **Browser Cockpit** — drag live role sessions into fixed panes and work without switching terminals.
- **tmux-backed persistence** — browser refreshes do not kill the agent session.
- **Editable by default** — Cockpit panes are writable native terminals, not read-only screenshots.
- **Bell / attention signals** — Codex approval state and Claude waiting/permission prompts surface in status.
- **tmux copy-mode scrolling** — Mac trackpad scrolling maps to tmux history instead of broken xterm scrollback.
- **Human acceptance gate** — agent output does not auto-complete a task; humans click Complete.

## What it looks like conceptually

```text
/cockpit

CONTROL / Sessions
  [analyst]    running   Codex
  [architect]  ready     Codex
  [developer]  todo      Codex
  [tester]     todo      Codex
  [ops]        blocked   Claude Code

Panes
  #1 analyst Codex TUI
  #2 ops Claude Code TUI
  #3 developer Codex TUI
```

Each pane is a real `ttyd -> tmux -> codex/claude` terminal.

## Requirements

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) with `hermes_cli.kanban_db` importable
- Python 3.11+
- `tmux`
- `ttyd`
- OpenAI Codex CLI for `provider: codex`
- Anthropic Claude Code CLI for `provider: claude`

On macOS:

```bash
brew install tmux ttyd
```

## Install as a Hermes plugin

Clone or download this repository, then install it into your Hermes profile:

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

Edit your private config:

```text
~/.hermes/kanban-agency/roles.yaml
~/.hermes/kanban-agency/rules/*.md
```

Keep those files private. They usually contain project paths, team conventions, and internal instructions.

## Configure roles

Example role mapping:

```yaml
roles:
  analyst:
    provider: codex
    rules:
      - ~/.hermes/kanban-agency/rules/analyst.md
  architect:
    provider: codex
    rules:
      - ~/.hermes/kanban-agency/rules/architect.md
  developer:
    provider: codex
    rules:
      - ~/.hermes/kanban-agency/rules/developer.md
  tester:
    provider: codex
    rules:
      - ~/.hermes/kanban-agency/rules/tester.md
  ops:
    provider: claude
    rules:
      - ~/.hermes/kanban-agency/rules/ops.md
```

## Basic workflow

Create a Hermes Kanban root task whose body contains `@kanban-agency`:

```text
@kanban-agency
workdir: /absolute/path/to/project
workflow: functional-development

Build a visible thumbs-down feedback flow for the AI assistant.
```

Then run:

```bash
scripts/kanban-agency scan --board my_board
scripts/kanban-agency start --board my_board
scripts/kanban-agency run --board my_board --task-id t_xxx
```

For `workflow: functional-development`, Kanban Agency creates a fixed role chain:

```text
analyst -> architect -> developer -> tester
```

Later roles are visible up front as `todo`; they become `ready` when upstream roles are completed.

## Browser Cockpit

Start the local gateway:

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

## Codex and Claude Code integration

### Codex

`provider: codex` starts:

```text
tmux session -> codex native TUI -> ttyd browser pane
```

Kanban Agency records the Codex thread id, tmux session, ttyd URL, and task status. Pending approval can be detected from Codex session logs and surfaced as a bell/attention state.

### Claude Code

`provider: claude` starts:

```text
tmux session -> claude native TUI -> ttyd browser pane
```

Claude Code does not expose the same structured approval JSON as Codex, so Kanban Agency detects attention state from the tmux screen: waiting prompt, permission prompt, or user-input prompt.

## Design principles

- **Kanban remains the source of truth.** Role sessions are execution surfaces; task state lives in Kanban.
- **Native tools stay native.** Codex and Claude Code run as real CLI/TUI sessions.
- **Human Complete is the acceptance gate.** A role can finish its work, but the next phase only proceeds after human acceptance.
- **Cockpit is a view/control layer.** It does not create a second task database.
- **Private rules stay private.** Use templates for public defaults and keep actual role instructions outside the repo.

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
49 passed
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
