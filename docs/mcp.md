# MCP Integration

## Positioning

`codex-dual-pipeline` 的主产品形态是一个 MCP server。

如果你想把它接到自己的 AI coding 工具里，应该优先集成 MCP，而不是依赖一段 prompt 或说明文档来“模拟调用”。

## Exposed Tools

当前 MCP server 暴露以下工具：

- `pipeline_loop`
- `start_pipeline_run`
- `get_pipeline_run`
- `tail_pipeline_log`
- `read_pipeline_artifact`

这些工具由 [dual_pipeline/mcp_server.py](../dual_pipeline/mcp_server.py) 实现。

## Recommended Usage

### Short or medium tasks

优先直接调用 `pipeline_loop`：

```json
{
  "repo": "/path/to/repo",
  "goal_file": "docs/task-brief.md"
}
```

适用场景：

- 任务可以在一次同步调用里完成
- 调用方希望直接拿到 `session_summary`
- 不需要单独轮询日志

### Long-running tasks

优先使用后台模式：

1. `start_pipeline_run`
2. `get_pipeline_run`
3. `tail_pipeline_log`
4. `read_pipeline_artifact`

适用场景：

- 任务可能超过同步调用超时
- 需要外层 agent 展示进度
- 需要分阶段追踪日志和中间结果

## Recommended Calling Rules

- 优先传 `goal_file`，只在没有仓库内任务文件时才传 `goal`
- `goal_file` 应位于目标仓库内部
- 默认优先 `pipeline_loop`
- 当任务可能较长、仓库较大或要求持续反馈时，改用后台模式
- 优先先读 `session_summary`
- 只有需要审查具体拒绝原因、后续任务或证据时，再读 `verdict`

## Typical Control Flow

### Synchronous flow

1. 调用 `pipeline_loop`
2. 读取返回的 `session_summary`
3. 必要时读取 `verdict`
4. 外层 agent 再决定是否继续与用户确认

### Background flow

1. 调用 `start_pipeline_run`
2. 轮询 `get_pipeline_run`
3. 需要时用 `tail_pipeline_log` 获取最近进展
4. 完成后读 `session_summary`
5. 需要细节时再读 `plan` / `result` / `verdict`

## Artifacts

MCP 返回的工件通常包括：

- `plan.json`
- `result.json`
- `verdict.json`
- `session_summary.json`

推荐外层 agent 的消费顺序：

1. `session_summary.json`
2. `verdict.json`
3. `result.json`
4. `plan.json`

## Integration Notes

- 这个项目默认假设目标目录是本地 Git 仓库
- `review` / `verify` 走只读 sandbox，`develop` 走可写 sandbox
- 如果你的宿主工具支持 MCP 但不支持 Skill，也不影响主流程使用
- 如果你的宿主工具同时支持 Skill，建议把 Skill 当作一层“调用策略增强”，而不是替代 MCP
