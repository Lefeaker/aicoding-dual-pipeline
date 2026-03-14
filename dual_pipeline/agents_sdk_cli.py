from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents import Agent, RunConfig, Runner, function_tool
from agents.mcp import MCPServerStdio
from pydantic import BaseModel, Field

from dual_pipeline.cli import (
    collect_repo_context,
    ensure_repo,
    load_json,
    prepare_artifact_dir,
    read_goal,
)


class PlanTask(BaseModel):
    id: str
    title: str
    priority: str
    files: list[str] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)


class PlanArtifact(BaseModel):
    goal: str
    proceed: bool
    summary: str
    tasks: list[PlanTask] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    command: str
    status: str
    details: str


class ResultArtifact(BaseModel):
    status: str
    summary: str
    completed_task_ids: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    validations: list[ValidationResult] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    follow_up: list[str] = Field(default_factory=list)


class NextTask(BaseModel):
    id: str
    title: str
    acceptance: list[str] = Field(default_factory=list)


class VerdictArtifact(BaseModel):
    status: str
    summary: str
    accepted_task_ids: list[str] = Field(default_factory=list)
    rejected_task_ids: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    next_tasks: list[NextTask] = Field(default_factory=list)


@dataclass
class AppConfig:
    repo: Path
    artifacts_dir: Path
    goal: str
    base: str | None
    codex_model: str | None
    agent_model: str | None
    mcp_timeout_seconds: float
    tracing_disabled: bool


@contextmanager
def quiet_root_logger(level: int = logging.ERROR):
    root = logging.getLogger()
    previous = root.level
    root.setLevel(level)
    try:
        yield
    finally:
        root.setLevel(previous)


class CodexMCPBridge:
    def __init__(self, *, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self.server: MCPServerStdio | None = None

    async def __aenter__(self) -> "CodexMCPBridge":
        self.server = MCPServerStdio(
            params={"command": "codex", "args": ["mcp-server"]},
            cache_tools_list=True,
            client_session_timeout_seconds=self.timeout_seconds,
            max_retry_attempts=1,
        )
        await self.server.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.server is not None:
            await self.server.__aexit__(exc_type, exc, tb)
            self.server = None

    async def run_codex(
        self,
        *,
        prompt: str,
        cwd: Path,
        sandbox: str,
        developer_instructions: str,
        model: str | None,
    ) -> str:
        if self.server is None:
            raise RuntimeError("Codex MCP bridge is not active")

        arguments: dict[str, Any] = {
            "prompt": prompt,
            "cwd": str(cwd),
            "sandbox": sandbox,
            "approval-policy": "never",
            "developer-instructions": developer_instructions,
        }
        if model:
            arguments["model"] = model

        with quiet_root_logger():
            result = await self.server.call_tool("codex", arguments)

        if result.isError:
            raise RuntimeError(f"codex tool error: {result}")

        structured = getattr(result, "structuredContent", None) or {}
        if isinstance(structured, dict) and structured.get("content"):
            return str(structured["content"])

        content = getattr(result, "content", None) or []
        text_parts: list[str] = []
        for item in content:
            item_type = getattr(item, "type", None)
            item_text = getattr(item, "text", None)
            if item_type == "text" and item_text:
                text_parts.append(item_text)
        return "\n".join(text_parts).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aicoding-dual-pipeline-agents",
        description="Run the dual-role pipeline through Agents SDK with a Codex MCP backend.",
    )
    parser.add_argument("--repo", required=True, help="Target git repository path.")
    parser.add_argument("--artifacts-dir", default="artifacts", help="Directory for JSON artifacts.")
    parser.add_argument("--goal", help="Short natural-language goal for this run.")
    parser.add_argument("--goal-file", help="Read the goal from a text or markdown file.")
    parser.add_argument("--base", help="Optional base branch for diff context, e.g. origin/main.")
    parser.add_argument("--codex-model", help="Optional model override passed to the Codex MCP tool.")
    parser.add_argument("--agent-model", help="Optional model override for the Agents SDK orchestrator.")
    parser.add_argument(
        "--mcp-timeout-seconds",
        type=float,
        default=300.0,
        help="Timeout for each Codex MCP tool call.",
    )
    parser.add_argument(
        "--enable-tracing",
        action="store_true",
        help="Enable Agents SDK tracing instead of disabling it by default.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("review", help="Run only the Reviewer / Planner agent.")
    subparsers.add_parser("develop", help="Run only the Developer / Executor agent.")
    subparsers.add_parser("verify", help="Run only the Reviewer / Verifier agent.")
    subparsers.add_parser("run", help="Run review, develop, and verify sequentially.")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> AppConfig:
    repo = ensure_repo(args.repo)
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required for the Agents SDK workflow")
    return AppConfig(
        repo=repo,
        artifacts_dir=prepare_artifact_dir(args.artifacts_dir),
        goal=read_goal(args),
        base=args.base,
        codex_model=args.codex_model,
        agent_model=args.agent_model,
        mcp_timeout_seconds=args.mcp_timeout_seconds,
        tracing_disabled=not args.enable_tracing,
    )


def run_config(config: AppConfig) -> RunConfig:
    return RunConfig(
        model=config.agent_model,
        workflow_name="AI Coding Dual Pipeline",
        tracing_disabled=config.tracing_disabled,
    )


def reviewer_tool_instructions() -> str:
    return (
        "You are the Codex Reviewer / Planner. Inspect the repository state and produce concise, "
        "evidence-based planning notes. Do not edit code. If shell access is needed, stay read-only."
    )


def developer_tool_instructions() -> str:
    return (
        "You are the Codex Developer / Executor. Implement only the approved plan, keep changes scoped, "
        "and run the smallest useful validation commands. You may edit files inside the repository."
    )


def verifier_tool_instructions() -> str:
    return (
        "You are the Codex Reviewer / Verifier. Inspect the implementation against the original plan, "
        "be strict about acceptance criteria, and do not edit code."
    )


def build_reviewer_codex_prompt(config: AppConfig, context: dict[str, str]) -> str:
    return f"""
Goal:
{config.goal}

Repository:
{config.repo}

Repository context:
{json.dumps(context, ensure_ascii=False, indent=2)}

Task:
- Review the current repository state.
- Identify the most important implementation tasks required to move the goal forward.
- Write concise evidence and recommendations for a separate developer.
- Do not write code.

Return plain text with:
1. summary
2. tasks
3. constraints
4. risks
""".strip()


def build_developer_codex_prompt(config: AppConfig, plan: PlanArtifact) -> str:
    return f"""
Goal:
{config.goal}

Approved plan:
{plan.model_dump_json(indent=2)}

Task:
- Implement the approved plan in the repository.
- Run targeted validation commands when useful.
- Return a concise execution report.

Return plain text with:
1. what changed
2. validation commands and outcomes
3. blockers
4. follow-up items
""".strip()


def build_verifier_codex_prompt(
    config: AppConfig,
    plan: PlanArtifact,
    result: ResultArtifact,
    context: dict[str, str],
) -> str:
    return f"""
Goal:
{config.goal}

Original plan:
{plan.model_dump_json(indent=2)}

Developer result:
{result.model_dump_json(indent=2)}

Repository context:
{json.dumps(context, ensure_ascii=False, indent=2)}

Task:
- Verify whether the implementation satisfies the plan.
- Be strict about acceptance criteria.
- If revision is needed, produce focused follow-up tasks.
- Do not edit code.

Return plain text with:
1. verdict
2. accepted tasks
3. rejected tasks
4. findings
5. next tasks
""".strip()


def reviewer_agent(bridge: CodexMCPBridge, config: AppConfig, context: dict[str, str]) -> Agent[Any]:
    @function_tool
    async def inspect_repo_with_codex() -> str:
        """Inspect the repository through Codex MCP in read-only mode and return review notes."""

        return await bridge.run_codex(
            prompt=build_reviewer_codex_prompt(config, context),
            cwd=config.repo,
            sandbox="read-only",
            developer_instructions=reviewer_tool_instructions(),
            model=config.codex_model,
        )

    return Agent(
        name="reviewer_agent",
        instructions=(
            "You are the Reviewer / Planner. Call `inspect_repo_with_codex` exactly once, then convert "
            "the returned notes into the required structured output. Do not invent repository details."
        ),
        tools=[inspect_repo_with_codex],
        output_type=PlanArtifact,
        model=config.agent_model,
    )


def developer_agent(bridge: CodexMCPBridge, config: AppConfig, plan: PlanArtifact) -> Agent[Any]:
    @function_tool
    async def implement_plan_with_codex() -> str:
        """Execute the approved plan through Codex MCP with workspace-write access."""

        return await bridge.run_codex(
            prompt=build_developer_codex_prompt(config, plan),
            cwd=config.repo,
            sandbox="workspace-write",
            developer_instructions=developer_tool_instructions(),
            model=config.codex_model,
        )

    return Agent(
        name="developer_agent",
        instructions=(
            "You are the Developer / Executor. Call `implement_plan_with_codex` exactly once, then "
            "convert the execution report into the required structured output. Keep task ids aligned "
            "with the approved plan when possible."
        ),
        tools=[implement_plan_with_codex],
        output_type=ResultArtifact,
        model=config.agent_model,
    )


def verifier_agent(
    bridge: CodexMCPBridge,
    config: AppConfig,
    plan: PlanArtifact,
    result: ResultArtifact,
    context: dict[str, str],
) -> Agent[Any]:
    @function_tool
    async def verify_with_codex() -> str:
        """Verify the implementation through Codex MCP in read-only mode."""

        return await bridge.run_codex(
            prompt=build_verifier_codex_prompt(config, plan, result, context),
            cwd=config.repo,
            sandbox="read-only",
            developer_instructions=verifier_tool_instructions(),
            model=config.codex_model,
        )

    return Agent(
        name="verifier_agent",
        instructions=(
            "You are the Reviewer / Verifier. Call `verify_with_codex` exactly once, then convert the "
            "verification notes into the required structured output. Be conservative about pass/fail."
        ),
        tools=[verify_with_codex],
        output_type=VerdictArtifact,
        model=config.agent_model,
    )


async def run_review(config: AppConfig, bridge: CodexMCPBridge) -> PlanArtifact:
    context = collect_repo_context(config.repo, config.base)
    agent = reviewer_agent(bridge, config, context)
    result = await Runner.run(agent, "Create the structured implementation plan.", run_config=run_config(config))
    artifact = result.final_output
    if not isinstance(artifact, PlanArtifact):
        raise RuntimeError("Reviewer agent did not return a PlanArtifact")
    write_artifact(config.artifacts_dir / "plan.json", artifact)
    return artifact


async def run_develop(config: AppConfig, bridge: CodexMCPBridge) -> ResultArtifact:
    plan_path = config.artifacts_dir / "plan.json"
    if not plan_path.exists():
        raise SystemExit(f"missing plan file: {plan_path}")
    plan = PlanArtifact.model_validate(load_json(plan_path))
    agent = developer_agent(bridge, config, plan)
    result = await Runner.run(agent, "Execute the approved plan.", run_config=run_config(config))
    artifact = result.final_output
    if not isinstance(artifact, ResultArtifact):
        raise RuntimeError("Developer agent did not return a ResultArtifact")
    write_artifact(config.artifacts_dir / "result.json", artifact)
    return artifact


async def run_verify(config: AppConfig, bridge: CodexMCPBridge) -> VerdictArtifact:
    plan_path = config.artifacts_dir / "plan.json"
    result_path = config.artifacts_dir / "result.json"
    if not plan_path.exists():
        raise SystemExit(f"missing plan file: {plan_path}")
    if not result_path.exists():
        raise SystemExit(f"missing result file: {result_path}")
    plan = PlanArtifact.model_validate(load_json(plan_path))
    result_artifact = ResultArtifact.model_validate(load_json(result_path))
    context = collect_repo_context(config.repo, config.base)
    agent = verifier_agent(bridge, config, plan, result_artifact, context)
    result = await Runner.run(agent, "Verify the implementation.", run_config=run_config(config))
    artifact = result.final_output
    if not isinstance(artifact, VerdictArtifact):
        raise RuntimeError("Verifier agent did not return a VerdictArtifact")
    write_artifact(config.artifacts_dir / "verdict.json", artifact)
    return artifact


def write_artifact(path: Path, model: BaseModel) -> None:
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")
    print(f"artifact: {path}")
    print(model.model_dump_json(indent=2))


async def async_main() -> None:
    args = parse_args()
    config = build_config(args)
    async with CodexMCPBridge(timeout_seconds=config.mcp_timeout_seconds) as bridge:
        if args.command == "review":
            await run_review(config, bridge)
            return
        if args.command == "develop":
            await run_develop(config, bridge)
            return
        if args.command == "verify":
            await run_verify(config, bridge)
            return
        await run_review(config, bridge)
        await run_develop(config, bridge)
        await run_verify(config, bridge)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
