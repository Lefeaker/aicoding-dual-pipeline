# codex-dual-pipeline

`codex-dual-pipeline` 是一个本地优先的 Codex 编排器，用来把“大任务交给一个代理一次做完”的流程，拆成更稳定、可复查的多阶段流水线。

项目定位是 `MCP + optional Skill`：

- `MCP` 是主产品：给外层 AI coding 工具暴露可调用能力
- `Skill` 是可选适配层：教外层 agent 什么时候、如何调用这些 MCP 工具

它把执行过程固定成几个职责清晰的角色：

- `review`: Reviewer / Planner，只读仓库，产出 `plan.json`
- `develop`: Developer / Executor，按 `plan.json` 改代码并回传 `result.json`
- `verify`: Reviewer / Verifier，只读仓库，复审并产出 `verdict.json`
- `loop`: 先生成总计划，然后在 Reviewer / Developer 之间循环直到完成

这套设计适合：

- 把 Codex 当作本地工程执行器，而不是聊天机器人
- 需要把“规划”和“改代码”强制分离
- 想把中间产物落盘，方便追查和复盘
- 想把一个内层流水线暴露给外层 Codex 或其他 agent 调用

## Why This Exists

很多实际任务不是“一次 prompt 解决”的问题，而是：

1. 先读仓库，收敛任务边界
2. 再按结构化计划实施
3. 再让一个独立 reviewer 检查结果
4. 如果没过，再下发下一轮小任务

这个项目就是把这套流程固化成一个可脚本化、可嵌入、可通过 MCP 暴露的本地工具。

## Key Features

- 三阶段固定职责：`review` / `develop` / `verify`
- `loop` 自动循环直到完成、阻塞或到达预算上限
- 所有阶段产物都落成 JSON 文件
- 用 JSON Schema 约束输出结构，降低自由发挥
- 支持直接 CLI 调用，也支持通过 MCP 暴露给外层 agent
- 支持后台长任务运行和日志轮询
- 可选 Skill，可为支持 skill/prompt-pack 的 agent 提供调用策略

## Product Model

这个仓库建议这样使用：

- 如果你在做工具集成，优先接 MCP
- 如果你的 agent 支持 Skill，再额外安装可选 Skill

两者分工：

- MCP 负责“提供能力”
- Skill 负责“提供调用策略”

核心原则：

- Reviewer 不直接改代码
- Developer 不重新规划需求
- 两端只通过结构化 JSON 工单交接

## Quick Start

安装并在一个本地 Git 仓库上跑完整三阶段：

```bash
cd /path/to/codex-dual-pipeline
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .

codex-dual-pipeline \
  --repo /path/to/repo \
  --goal "修复登录态失效问题并补齐回归测试" \
  run
```

如果你要把它接到外层 agent，优先看：

- [docs/mcp.md](docs/mcp.md)
- [docs/skill.md](docs/skill.md)
- [skill/SKILL.md](skill/SKILL.md)

## 依赖

- 已安装并可运行的 `codex` CLI
- 已登录 Codex
- Python 3.11+
- 一个本地 Git 仓库
- 如果使用 Agents SDK 版本，还需要 `OPENAI_API_KEY`

## 安装

```bash
cd /path/to/codex-dual-pipeline
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

如果你不想安装成命令，也可以直接用模块方式运行：

```bash
cd /path/to/codex-dual-pipeline
python3 -m dual_pipeline.cli --repo /path/to/repo --goal "修复登录态失效问题" run
```

Agents SDK + Codex MCP 版本：

```bash
cd /path/to/codex-dual-pipeline
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
export OPENAI_API_KEY=...
python3 -m dual_pipeline.agents_sdk_cli --repo /path/to/repo --goal "修复登录态失效问题" run
```

把循环工具暴露给外层 Codex 作为 MCP server：

```bash
cd /path/to/codex-dual-pipeline
source .venv/bin/activate
codex mcp add dual-pipeline -- /path/to/codex-dual-pipeline/.venv/bin/python -m dual_pipeline.mcp_server
```

## 用法

只跑计划阶段：

```bash
codex-dual-pipeline \
  --repo /path/to/repo \
  --goal "修复登录态失效问题" \
  review
```

串行跑完整三阶段：

```bash
codex-dual-pipeline \
  --repo /path/to/repo \
  --goal "修复登录态失效问题" \
  run
```

跑你描述的“计划检查节点 <-> 执行节点循环”：

```bash
python3 -m dual_pipeline.cli \
  --repo /path/to/repo \
  --goal-file /path/to/brief.md \
  loop
```

如果不传 `--max-iterations`，工具会先让一个只读 Codex 读一遍 `goal_file` 和 repo 状态，自动估算这次 loop 的预算次数。

指定基线分支：

```bash
codex-dual-pipeline \
  --repo /path/to/repo \
  --goal "补齐 401 refresh retry 测试" \
  --base origin/main \
  run
```

只看将执行什么命令，不真正调用 Codex：

```bash
codex-dual-pipeline \
  --repo /path/to/repo \
  --goal "修复登录态失效问题" \
  --dry-run \
  run
```

## 产物

默认写到当前目录下的 `artifacts/`：

- `plan.json`
- `result.json`
- `verdict.json`
- `session_summary.json`

## 两套实现的区别

`dual_pipeline.cli`:

- 直接用 `codex exec`
- 最稳，最适合脚本、CI、批处理
- 不依赖 `OPENAI_API_KEY`
- 现在支持 `loop`，更适合被“第一个 Codex”当工具调用

`dual_pipeline.agents_sdk_cli`:

- 用 Agents SDK 编排三个专职 agent
- 每个 agent 通过 `codex mcp-server` 调 Codex
- 更接近长期可扩展的多 agent 系统
- 需要 `OPENAI_API_KEY`

## 设计说明

- `review` 和 `verify` 使用 `codex exec --sandbox read-only`
- `develop` 使用 `codex exec --sandbox workspace-write`
- 三个阶段都通过 `--output-schema` 强约束最终输出形状
- 所有上下文只从 Git 状态、diff 摘要、最近提交和上游 JSON 产物中构造
- `loop` 的逻辑是：
  - 第一次 `review` 生成总计划
  - 然后反复执行 `develop -> verify`
  - 如果 `verify` 返回 `next_tasks`，它们会被写回新的 `plan.json`
  - 直到 `pass`、`blocked`、没有后续任务、或达到 `--max-iterations`

## 你理想中的使用方式

推荐把这个工具当成“中间协调器”，由第一个 Codex 直接调用：

1. 第一个 Codex 接收你的问题诊断或大任务清单
2. 第一个 Codex 调用：

```bash
python3 -m dual_pipeline.cli \
  --repo /path/to/repo \
  --goal-file /path/to/brief.md \
  --max-iterations 8 \
  loop
```

3. 工具内部完成：
   - Reviewer / Planner 生成首轮任务
   - Developer / Executor 执行
   - Reviewer / Verifier 审核并下发下一轮小任务
   - 反复循环直到完成
4. 第一个 Codex 最后读取 `artifacts/session_summary.json`、`verdict.json`，再决定是否向你确认“整个清单已完成”

## MCP 用法

注册后，第一个 Codex 可以直接调用两个工具：

- `pipeline_loop`
- `start_pipeline_run`
- `get_pipeline_run`
- `tail_pipeline_log`
- `read_pipeline_artifact`

典型调用参数：

```json
{
  "repo": "/path/to/repo",
  "goal_file": "docs/task-brief.md"
}
```

`goal_file` 推荐优先使用：

- 必须位于目标 repo 内
- 可以写相对路径，按 repo 根目录解析
- 适合让第一个 Codex 直接把“仓库里的问题说明文件”交给工具

只有在没有现成文件时，才传 `goal`

`max_iterations` 现在是可选：

- 不传：由内层 Codex 自动预估，并默认额外留一轮冗余
- 传了：使用你指定的上限覆盖自动预估

`pipeline_loop` 返回：

- `artifacts_dir`
- `session_summary`
- `plan_path`
- `result_path`
- `verdict_path`
- `stdout`

如果任务可能超过 120 秒，不要用 `pipeline_loop`，改用后台作业模式：

1. `start_pipeline_run`
2. 轮询 `get_pipeline_run`
3. 需要时调用 `tail_pipeline_log`
4. 完成后调用 `read_pipeline_artifact`

长任务推荐参数：

```json
{
  "repo": "/path/to/repo",
  "goal_file": "docs/task-brief.md"
}
```

`start_pipeline_run` 会立即返回：

- `run_id`
- `status`
- `artifacts_dir`
- `log_path`
- `run_metadata_path`

之后用 `run_id` 轮询：

```json
{
  "run_id": "your-run-id"
}
```

如果要看最近日志：

```json
{
  "run_id": "your-run-id",
  "max_lines": 80
}
```

如果第一个 Codex 还想继续追读细节，再调用 `read_pipeline_artifact`：

```json
{
  "artifacts_dir": "/path/to/artifacts-dir",
  "name": "verdict"
}
```

## 开源建议

如果准备公开仓库，建议不要提交以下内容：

- `runs/` 中的历史运行记录和日志
- `artifacts/` 中的阶段产物
- `*.egg-info/`、`__pycache__/`、`.DS_Store`
- 任何包含真实本地绝对路径、仓库名或任务内容的示例输出

## License

MIT. 见 [LICENSE](LICENSE)。

## 后续演进

如果你要把它升级成你描述的“更像两个会话在对话”的系统，下一步建议是：

1. 保留当前 JSON schema，不动交接协议
2. 把 `review/develop/verify` 这三个 prompt 抽成独立模板文件
3. 为 Reviewer / Developer / Verifier 分别保留长生命周期 thread id
4. 再补 orchestrator agent 的 handoff / trace 可视化
