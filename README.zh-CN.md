# Kanban Agency

[English](README.md) | [简体中文](README.zh-CN.md)

**面向可信 multi-agent 软件工作的 Hermes 原生 Cockpit。**

Kanban Agency 把 [Hermes Agent](https://github.com/NousResearch/hermes-agent) Kanban 任务变成可审计的多 Agent 工作流：一个真实需求可以拆成 analyst、architect、developer、tester、researcher、ops 等角色；每个角色继续运行在自己的原生 Codex CLI、Claude Code 或 Hermes 会话里；整个执行现场则可以从 Kanban 看板恢复。

> Hermes Agent 负责事实来源：任务、依赖、评论和人工验收。Kanban Agency 在此之上增加 role 编排、tmux/ttyd 会话持久化、浏览器 Cockpit 控制面，以及 session 生命周期治理。

![Kanban Agency 把 Hermes 任务、角色、Agent 会话和恢复状态连接起来](docs/assets/01-kanban-agency-overview.png)

## 为什么需要 Kanban Agency

AI 编程工作流的问题通常不是 Agent 太少，而是工作越来越难监管：

- 一个巨大聊天上下文会隐藏决策和证据；
- 一堆终端会话会脱离任务状态；
- 需求、设计、实现、测试、运维之间的角色边界会漂移；
- Agent 自己说完成，容易被误当成人工验收；
- 旧 TUI session 泄漏后会拖慢整台机器；
- 人工纠正如果只停留在聊天里，下次仍然会重复犯错。

Kanban Agency 是“手工调度 Agent”和“盲目全自动”之间的一层工程化结构。它允许人在判断还不稳定的地方介入，同时把每次介入变成可观察、可恢复、可沉淀、可自动化的工作流信号。

```text
Hermes Kanban root task
  -> role cards: analyst -> architect -> developer -> tester
  -> native agent sessions: Codex CLI / Claude Code / Hermes
  -> tmux 持久化 + ttyd 浏览器 pane
  -> comments / events / dependencies / Complete gates / session health
```

## 为什么和 Hermes 关联紧密

Kanban Agency 不重新发明工作流数据库，而是刻意围绕 Hermes 构建：

| Hermes 层 | Kanban Agency 增加 |
| --- | --- |
| Kanban 看板、任务、依赖、评论 | role-aware 多 Agent 工作流结构 |
| 持久任务状态和审计历史 | 原生 TUI 会话绑定和恢复 |
| Human-in-the-loop Complete gate | 浏览器 Cockpit，用于观察和接管 live session |
| Hermes sessions、memory、skills、tools | 把重复人工纠正沉淀为 role rules 的入口 |

这样，Kanban board 就成为 Agent 工作的持久索引：即使浏览器标签、终端或 provider 进程消失，任务仍然知道自己对应哪个 role、哪个 provider thread，以及如何恢复现场。

![架构：外层是 Hermes Kanban，内层保留原生 Agent TUI](docs/assets/05-kanban-agency-architecture.png)

## 亮点

- **Hermes 原生 Kanban 工作流**：任务、评论、依赖和审计历史仍然留在 Hermes Kanban。
- **角色化多 Agent 流程**：功能开发可预创建 analyst -> architect -> developer -> tester 角色链。
- **原生 provider session**：Codex CLI、Claude Code、Hermes 都以真实 terminal/TUI 方式运行。
- **浏览器 Cockpit**：把多个 live role session 拖进固定 pane，减少终端切换。
- **tmux 持久化**：浏览器刷新不会杀掉 Agent 会话；停止的 session 可从 task 恢复。
- **Attention 状态**：Codex approval、Claude/Hermes 等待输入会暴露成 bell/status badge。
- **人工验收门禁**：Agent 完成 role 后，下游仍等待人工 Complete。
- **Recent activity 索引**：root 按真实 task/provider 活动排序，而不是只看创建时间。
- **Session 生命周期治理**：超过阈值的已完成 session 可关闭，orphan ttyd/tmux wrapper 可报告和清理。
- **私有 role rules**：团队/项目规则留在 Hermes profile，公开仓库只发布安全模板。

![角色、provider 和私有规则文件共同组成稳定执行语境](docs/assets/03-kanban-agency-role-rules-harness.png)

## 概念效果

```text
Cockpit

Recent
  Agent file upload                           🔔
    analyst     done       Codex
    architect   done       Codex
    developer   running    Codex
    tester      ready      Codex

Panes
  #1 developer Codex TUI
  #2 tester Codex TUI
  #3 ops Claude Code TUI
```

每个 pane 都是真实的：

```text
browser -> ttyd -> tmux -> codex / claude / hermes
```

你可以进入 live terminal、滚动 tmux 历史、处理 approval、人工补充指令、标记 Complete，也可以之后从同一个 task 重新打开现场追溯证据。

## 设计原则

- **Kanban 是事实来源**：role session 是执行面，任务状态留在 Hermes。
- **保留原生工具体验**：不重写 Codex、Claude Code、Hermes、tools、skills 或模型 runtime。
- **人工介入是能力，不是失败**：在自动化稳定前，流程必须可观察、可纠错、可接管。
- **Complete 是安全边界**：provider 完成不等于任务被验收。
- **会话恢复是基础能力**：每个 live terminal 都要能追溯到 task、role、provider、workdir 和 thread id。
- **重复纠正应该变成规则**：用户拒绝、用户补充、用户引导都应该成为改进 role prompt 和 workflow gate 的信号。

![会话恢复：每个 live terminal 都应该能追溯到 task、role、provider 和 thread id](docs/assets/04-kanban-agency-session-index.png)

## 常见使用场景

- 用 analyst / architect / developer / tester 分工交付功能。
- 需要长期运行、可恢复的 Codex 或 Claude 会话。
- Agent 负责分析、实现、测试，但由人决定何时推进的自动化流程。
- 调研、运维、review 等多独立 Agent 终端并行，但仍需要任务级审计历史的场景。
- 团队想探索 multi-agent 自动编排，同时不放弃现有 Hermes、Codex、Claude 工作方式。

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

Kanban Agency 是一个 Hermes plugin。推荐用 Hermes 自带的 plugin manager 安装，这样 Hermes 可以统一 clone、enable、disable 和 update：

```bash
hermes plugins install <owner>/kanban-agency --enable
# 或使用完整 Git URL：
hermes plugins install https://github.com/<owner>/kanban-agency.git --enable
```

本地开发时，可以把 checkout 复制或软链到当前 Hermes profile：

```bash
mkdir -p ~/.hermes/plugins
ln -s /absolute/path/to/kanban-agency ~/.hermes/plugins/kanban-agency
# 或者复制一份：
# cp -R /absolute/path/to/kanban-agency ~/.hermes/plugins/kanban-agency
hermes plugins enable kanban-agency
```

检查插件状态：

```bash
hermes plugins list --plain --no-bundled
hermes plugins enable kanban-agency
hermes plugins disable kanban-agency
hermes plugins update kanban-agency
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

![典型流程：root task、role cards、原生 session、attention、Complete gate](docs/assets/07-kanban-agency-workflow.png)

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
