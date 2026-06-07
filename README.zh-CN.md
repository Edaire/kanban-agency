# Kanban Agency

[English](README.md) | [简体中文](README.zh-CN.md)

**面向 Hermes Agent、OpenAI Codex CLI 和 Anthropic Claude Code 的 Kanban 原生多 Agent 编排工具。**

Kanban Agency 可以把一个 Hermes Kanban 看板变成实时 AI Agent 工作台：把一个需求拆成角色，让每个角色运行在自己的原生 Codex 或 Claude Code 终端里，同时用 Kanban 记录任务状态、依赖、评论和审计历史。

> Hermes Agent 提供 Kanban/任务层；Codex 和 Claude Code 提供原生 Agent 会话；Kanban Agency 用 tmux、ttyd 和浏览器 Cockpit 把它们连接起来。

## 为什么需要它

常见 AI 编程工作流往往会变成：

- 一个巨大聊天上下文，难以审计和交接；
- 一堆终端会话，没有清晰任务状态；
- 一个自定义 UI，把 Codex / Claude Code 的原生体验藏起来。

Kanban Agency 的目标是保留原生工具体验，同时加上工作流结构：

```text
Hermes Kanban task
  -> role card: analyst / architect / developer / tester / ops
  -> tmux 里的原生 Codex CLI 或 Claude Code 会话
  -> 通过 ttyd 显示到浏览器 Cockpit pane
  -> Kanban 状态、评论、依赖和人工 Complete 验收
```

## 亮点

- **Hermes 原生 Kanban 工作流**：任务、评论、依赖和审计历史仍然留在 Hermes Kanban。
- **角色化多 Agent 流程**：功能开发可预创建 analyst -> architect -> developer -> tester 角色链。
- **支持 OpenAI Codex CLI**：以持久原生 TUI 会话运行 Codex，不是假 UI 包装。
- **支持 Anthropic Claude Code**：适合 ops、review、交互式协作等角色。
- **浏览器 Cockpit**：把多个 live role session 拖进固定 pane，减少终端切换。
- **tmux 持久化**：浏览器刷新不会杀掉 Agent 会话。
- **默认可编辑**：Cockpit pane 是 writable 原生终端，不是只读截图。
- **Bell / attention 状态**：Codex approval 和 Claude 等待输入/权限提示会暴露到状态里。
- **tmux copy-mode 滚动**：Mac 触控板滚动映射到 tmux 历史，绕开 xterm scrollback 限制。
- **人工验收门禁**：Agent 输出不等于任务完成；仍需要人点击 Complete。

## 概念效果

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

每个 pane 都是真实的：

```text
ttyd -> tmux -> codex/claude
```

## 依赖

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)，且可以 import `hermes_cli.kanban_db`
- Python 3.11+
- `tmux`
- `ttyd`
- `provider: codex` 需要 OpenAI Codex CLI
- `provider: claude` 需要 Anthropic Claude Code CLI

macOS：

```bash
brew install tmux ttyd
```

## 安装为 Hermes 插件

克隆或下载本仓库后，安装到 Hermes profile：

```bash
mkdir -p ~/.hermes/plugins
cp -R . ~/.hermes/plugins/kanban-agency
```

创建私有角色配置和规则文件：

```bash
mkdir -p ~/.hermes/kanban-agency/rules
cp templates/roles.yaml.template ~/.hermes/kanban-agency/roles.yaml
for f in templates/rules/*.template; do
  name="$(basename "$f" .template)"
  cp "$f" "$HOME/.hermes/kanban-agency/rules/$name"
done
```

编辑你的私有配置：

```text
~/.hermes/kanban-agency/roles.yaml
~/.hermes/kanban-agency/rules/*.md
```

这些文件通常包含项目路径、团队约定和内部指令，不建议提交到公开仓库。

## 配置角色

示例：

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

## 基础工作流

创建一个 Hermes Kanban root task，body 中包含 `@kanban-agency`：

```text
@kanban-agency
workdir: /absolute/path/to/project
workflow: functional-development

Build a visible thumbs-down feedback flow for the AI assistant.
```

然后运行：

```bash
scripts/kanban-agency scan --board my_board
scripts/kanban-agency start --board my_board
scripts/kanban-agency run --board my_board --task-id t_xxx
```

对于 `workflow: functional-development`，Kanban Agency 会创建固定角色链：

```text
analyst -> architect -> developer -> tester
```

后续角色会提前可见为 `todo`；上游完成后，下游变为 `ready`。

## 浏览器 Cockpit

启动本地 gateway：

```bash
scripts/kanban-agency codex-web-gateway --port 8766
```

打开：

```text
http://127.0.0.1:8766/cockpit
```

常用路由：

```text
/cockpit                    多会话 Cockpit
/s/<task_id>                可写的单会话页面
/status/<task_id>           session/task 状态 JSON
/sessions                   所有 session 摘要
/sessions/<board>           指定 board 的 session 摘要
/resume/<task_id>           恢复/重建停止的 session
/tmux-scroll/<task_id>      ttyd wheel handler 使用的 tmux copy-mode 滚动接口
```

## Codex 和 Claude Code 接入

### Codex

`provider: codex` 启动：

```text
tmux session -> codex native TUI -> ttyd browser pane
```

Kanban Agency 会记录 Codex thread id、tmux session、ttyd URL 和任务状态。Codex pending approval 可从 session log 中识别，并显示为 bell/attention 状态。

### Claude Code

`provider: claude` 启动：

```text
tmux session -> claude native TUI -> ttyd browser pane
```

Claude Code 没有和 Codex 一样的结构化 approval JSON，所以 Kanban Agency 会从 tmux 屏幕内容识别 attention 状态，例如等待输入、权限确认或用户提示。

## 设计原则

- **Kanban 是事实来源**：role session 是执行面，任务状态留在 Kanban。
- **保留原生工具体验**：Codex 和 Claude Code 都运行在真实 CLI/TUI 会话里。
- **人工 Complete 是验收门禁**：角色完成工作后，仍需要人工确认再进入下一阶段。
- **Cockpit 是视图和控制层**：不创建第二套任务数据库。
- **私有规则保持私有**：公开仓库只放模板，真实角色指令放到本地私有配置。

## 本地/私有文件

不要提交运行状态或私有角色配置。`.gitignore` 已排除常见本地文件：

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

共享默认配置请使用 `templates/` 和 `examples/`。

## 测试

测试独立仓库时，需要让 Hermes Agent 可 import：

```bash
PYTHONPATH=/path/to/hermes-agent python -m pytest tests -q
```

当前本地验证：

```text
49 passed
```

## 开源前安全检查

公开推送前建议执行：

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

第三方 ttyd/xterm bundle 里可能会出现 `password` 等通用变量名，需要人工判断是否是真实敏感信息。

## License

MIT
