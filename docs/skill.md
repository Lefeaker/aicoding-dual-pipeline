# Optional Skill

## Purpose

这个 Skill 不是主产品，而是 `codex-dual-pipeline` 的可选适配层。

它的作用不是替代 MCP，而是帮助支持 Skill 的 agent 更稳定地调用这个 MCP。

## When To Use It

适合这些场景：

- 外层 agent 已经接入了本项目的 MCP
- 你希望 agent 自动选择同步模式还是后台模式
- 你希望 agent 先读 `session_summary` 再决定要不要追读其他 artifact
- 你希望 agent 避免把 reviewer 和 developer 职责混进外层 prompt

## What The Skill Should Do

这个 Skill 主要提供这些规则：

- 优先使用仓库内的 `goal_file`
- 短任务优先 `pipeline_loop`
- 长任务优先后台模式
- 完成后优先读 `session_summary`
- 只有必要时再读 `verdict` / `result` / `plan`
- 不要求外层 agent 自己重新规划 reviewer/developer/verifier 的职责

## What The Skill Should Not Do

- 不替代 MCP 工具本身
- 不复制 MCP 的所有输入输出定义
- 不把仓库文档全部塞进 Skill
- 不要求 agent 手写整套流水线 prompt

## Recommended Install Model

建议公开仓库时这样呈现：

- MCP 是默认安装项
- Skill 是 optional add-on

对外描述可以写成：

- “Use the MCP for execution; install the optional Skill for better orchestration guidance.”

## File

可安装 Skill 定义见 [skill/SKILL.md](../skill/SKILL.md)。
