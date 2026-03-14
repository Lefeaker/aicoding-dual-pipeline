# Contributing

## Scope

欢迎提交 issue 和 pull request，但请尽量保持项目目标收敛：

- 优先改进 `review` / `develop` / `verify` / `loop` 的稳定性
- 优先补足可验证的行为，而不是增加概念层
- 新功能应尽量保持本地优先、脚本友好、结构化输出

## Development

```bash
cd /path/to/aicoding-dual-pipeline
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

如果你修改了 CLI、MCP 或 artifact 结构，请同时更新：

- `README.md`
- `schemas/`
- 相关示例命令或返回值说明

## Pull Requests

提交 PR 时请尽量包含：

- 改动目标和背景
- 关键设计取舍
- 手动验证步骤
- 如果改了输出结构，说明兼容性影响

## Style

- 保持改动范围小而清晰
- 不要把无关重构混进同一个 PR
- 新行为尽量落成结构化输出，而不是只改提示词描述
