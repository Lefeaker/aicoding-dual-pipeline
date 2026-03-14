---
name: codex-dual-pipeline
description: Use this skill when an agent already has access to the codex-dual-pipeline MCP and needs guidance on when and how to call its tools for structured AI coding workflows. It is for choosing between pipeline_loop and background run mode, preferring goal_file over goal, reading session_summary first, and keeping reviewer, developer, and verifier responsibilities separated.
---

# Codex Dual Pipeline

Use this skill only when the `codex-dual-pipeline` MCP is available.

## Purpose

Guide the outer agent to call the pipeline tools in a stable order. Do not replace the MCP with hand-written prompts.

## Rules

- Prefer `goal_file` over inline `goal` when a repo file already exists
- Use `pipeline_loop` for short and medium tasks
- Use `start_pipeline_run` for long-running tasks or when progress polling is needed
- After completion, read `session_summary` first
- Read `verdict` only when you need rejection reasons, follow-up tasks, or review evidence
- Keep the outer prompt focused on task intent; do not merge reviewer and developer responsibilities into one free-form instruction block

## Recommended Flow

### Default flow

1. Call `pipeline_loop`
2. Inspect `session_summary`
3. If needed, inspect `verdict`
4. Summarize the outcome for the user

### Long-running flow

1. Call `start_pipeline_run`
2. Poll with `get_pipeline_run`
3. Read recent progress with `tail_pipeline_log` when useful
4. When done, read `session_summary`
5. If needed, inspect `verdict`, `result`, or `plan`

## Constraints

- Assume the target is a local Git repository
- Prefer repo-relative task briefs
- Do not treat the Skill as the executable interface; the MCP is the executable interface
