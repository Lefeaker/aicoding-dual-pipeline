from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server


ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = ROOT / "runs"


PIPELINE_LOOP_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["repo"],
    "properties": {
        "repo": {
            "type": "string",
            "description": "Absolute or relative path to the target git repository.",
        },
        "goal": {
            "type": "string",
            "description": "Task brief, diagnosis note, or high-level plan that the pipeline should execute.",
        },
        "goal_file": {
            "type": "string",
            "description": (
                "Path to a task brief file. If relative, it is resolved against the target repository. "
                "Use this instead of `goal` when the outer Codex should point at an existing repo file."
            ),
        },
        "base": {
            "type": "string",
            "description": "Optional base branch for diff context, such as origin/main.",
        },
        "max_iterations": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional override for reviewer/developer loop iterations. If omitted, the inner Codex estimates it from the task brief.",
        },
        "artifacts_dir": {
            "type": "string",
            "description": "Optional explicit artifacts directory. If omitted, a temporary run directory is created.",
        },
        "model": {
            "type": "string",
            "description": "Optional Codex model override passed to the inner pipeline.",
        },
        "dry_run": {
            "type": "boolean",
            "default": False,
            "description": "If true, print the inner codex commands without executing them.",
        },
    },
}

PIPELINE_LOOP_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "repo",
        "artifacts_dir",
        "command",
        "session_summary_path",
        "stdout",
    ],
    "properties": {
        "status": {"type": "string"},
        "repo": {"type": "string"},
        "artifacts_dir": {"type": "string"},
        "goal_file": {"type": "string"},
        "goal_preview": {"type": "string"},
        "command": {"type": "array", "items": {"type": "string"}},
        "effective_max_iterations": {"type": ["integer", "null"]},
        "session_summary_path": {"type": "string"},
        "plan_path": {"type": "string"},
        "result_path": {"type": "string"},
        "verdict_path": {"type": "string"},
        "stdout": {"type": "string"},
        "session_summary": {"type": ["object", "null"]},
    },
}

READ_ARTIFACT_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["artifacts_dir", "name"],
    "properties": {
        "artifacts_dir": {
            "type": "string",
            "description": "Artifacts directory returned from pipeline_loop.",
        },
        "name": {
            "type": "string",
            "enum": ["plan", "result", "verdict", "session_summary"],
            "description": "Artifact name to read.",
        },
    },
}

START_PIPELINE_RUN_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["repo"],
    "properties": PIPELINE_LOOP_INPUT_SCHEMA["properties"],
}

START_PIPELINE_RUN_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "run_id",
        "status",
        "repo",
        "artifacts_dir",
        "goal_file",
        "log_path",
        "run_metadata_path",
    ],
    "properties": {
        "run_id": {"type": "string"},
        "status": {"type": "string"},
        "repo": {"type": "string"},
        "artifacts_dir": {"type": "string"},
        "goal_file": {"type": "string"},
        "goal_preview": {"type": "string"},
        "log_path": {"type": "string"},
        "run_metadata_path": {"type": "string"},
        "pid": {"type": "integer"},
    },
}

GET_PIPELINE_RUN_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["run_id"],
    "properties": {
        "run_id": {
            "type": "string",
            "description": "Run id returned by start_pipeline_run.",
        },
    },
}

GET_PIPELINE_RUN_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
}

TAIL_PIPELINE_LOG_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["run_id"],
    "properties": {
        "run_id": {"type": "string"},
        "max_lines": {
            "type": "integer",
            "minimum": 1,
            "maximum": 400,
            "default": 80,
        },
    },
}

TAIL_PIPELINE_LOG_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["run_id", "log_path", "content"],
    "properties": {
        "run_id": {"type": "string"},
        "log_path": {"type": "string"},
        "content": {"type": "string"},
    },
}

READ_ARTIFACT_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "exists", "name", "content"],
    "properties": {
        "path": {"type": "string"},
        "exists": {"type": "boolean"},
        "name": {"type": "string"},
        "content": {"type": ["object", "array", "string", "number", "boolean", "null"]},
    },
}


def build_server() -> Server:
    server = Server(
        "codex-dual-pipeline-mcp",
        version="0.1.0",
        instructions=(
            "Use pipeline_loop to run the local reviewer/developer verification loop against a git repo. "
            "Use read_pipeline_artifact to inspect plan/result/verdict/session_summary."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="pipeline_loop",
                description=(
                    "Run the local Codex reviewer/developer loop for a repository and return the artifact paths "
                    "plus the final session summary when available."
                ),
                inputSchema=PIPELINE_LOOP_INPUT_SCHEMA,
                outputSchema=PIPELINE_LOOP_OUTPUT_SCHEMA,
            ),
            types.Tool(
                name="start_pipeline_run",
                description=(
                    "Start the pipeline loop as a background job for long-running tasks. "
                    "Returns immediately with a run_id that can be polled."
                ),
                inputSchema=START_PIPELINE_RUN_INPUT_SCHEMA,
                outputSchema=START_PIPELINE_RUN_OUTPUT_SCHEMA,
            ),
            types.Tool(
                name="get_pipeline_run",
                description="Get the current status of a background pipeline run.",
                inputSchema=GET_PIPELINE_RUN_INPUT_SCHEMA,
                outputSchema=GET_PIPELINE_RUN_OUTPUT_SCHEMA,
            ),
            types.Tool(
                name="tail_pipeline_log",
                description="Read the latest lines from a background pipeline run log.",
                inputSchema=TAIL_PIPELINE_LOG_INPUT_SCHEMA,
                outputSchema=TAIL_PIPELINE_LOG_OUTPUT_SCHEMA,
            ),
            types.Tool(
                name="read_pipeline_artifact",
                description="Read a JSON artifact emitted by pipeline_loop.",
                inputSchema=READ_ARTIFACT_INPUT_SCHEMA,
                outputSchema=READ_ARTIFACT_OUTPUT_SCHEMA,
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "pipeline_loop":
            return await run_pipeline_loop(arguments)
        if name == "start_pipeline_run":
            return start_pipeline_run(arguments)
        if name == "get_pipeline_run":
            return get_pipeline_run(arguments)
        if name == "tail_pipeline_log":
            return tail_pipeline_log(arguments)
        if name == "read_pipeline_artifact":
            return read_pipeline_artifact(arguments)
        raise ValueError(f"unknown tool: {name}")

    return server


def artifact_path(artifacts_dir: Path, name: str) -> Path:
    mapping = {
        "plan": "plan.json",
        "result": "result.json",
        "verdict": "verdict.json",
        "session_summary": "session_summary.json",
    }
    return artifacts_dir / mapping[name]


def read_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare_run_inputs(arguments: dict[str, Any]) -> tuple[Path, Path, Path, str]:
    repo = Path(str(arguments["repo"])).expanduser().resolve()
    if not repo.exists():
        raise ValueError(f"repo does not exist: {repo}")
    if not (repo / ".git").exists():
        raise ValueError(f"repo is not a git repository: {repo}")

    has_goal = bool(arguments.get("goal"))
    has_goal_file = bool(arguments.get("goal_file"))
    if has_goal == has_goal_file:
        raise ValueError("provide exactly one of `goal` or `goal_file`")

    if arguments.get("artifacts_dir"):
        artifacts_dir = Path(str(arguments["artifacts_dir"])).expanduser().resolve()
        artifacts_dir.mkdir(parents=True, exist_ok=True)
    else:
        artifacts_dir = Path(tempfile.mkdtemp(prefix="codex-dual-pipeline-"))

    if has_goal_file:
        raw_goal_file = Path(str(arguments["goal_file"]))
        goal_file = raw_goal_file if raw_goal_file.is_absolute() else (repo / raw_goal_file)
        goal_file = goal_file.expanduser().resolve()
        if not goal_file.exists():
            raise ValueError(f"goal_file does not exist: {goal_file}")
        try:
            goal_file.relative_to(repo)
        except ValueError as exc:
            raise ValueError(f"goal_file must be inside repo: {goal_file}") from exc
        goal_preview = goal_file.read_text(encoding="utf-8")
    else:
        goal_file = artifacts_dir / "goal.md"
        goal_preview = str(arguments["goal"])
        goal_file.write_text(goal_preview, encoding="utf-8")

    return repo, artifacts_dir, goal_file, goal_preview


def pipeline_command(
    *,
    repo: Path,
    artifacts_dir: Path,
    goal_file: Path,
    arguments: dict[str, Any],
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "dual_pipeline.cli",
        "--repo",
        str(repo),
        "--goal-file",
        str(goal_file),
        "--artifacts-dir",
        str(artifacts_dir),
    ]
    if arguments.get("max_iterations") is not None:
        cmd.extend(["--max-iterations", str(int(arguments["max_iterations"]))])
    if arguments.get("base"):
        cmd.extend(["--base", str(arguments["base"])])
    if arguments.get("model"):
        cmd.extend(["--model", str(arguments["model"])])
    if bool(arguments.get("dry_run", False)):
        cmd.append("--dry-run")
    cmd.append("loop")
    return cmd


async def run_pipeline_loop(arguments: dict[str, Any]) -> dict[str, Any]:
    repo, artifacts_dir, goal_file, goal_preview = prepare_run_inputs(arguments)
    cmd = pipeline_command(repo=repo, artifacts_dir=artifacts_dir, goal_file=goal_file, arguments=arguments)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=os.environ.copy(),
    )
    stdout_bytes, _ = await process.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")

    session_summary_path = artifact_path(artifacts_dir, "session_summary")
    session_summary = read_json_if_exists(session_summary_path)
    if process.returncode == 0:
        status = "ok"
        if isinstance(session_summary, dict) and isinstance(session_summary.get("status"), str):
            status = str(session_summary["status"])
        elif bool(arguments.get("dry_run", False)):
            status = "dry_run"
    else:
        status = f"process_error:{process.returncode}"

    return {
        "status": status,
        "repo": str(repo),
        "artifacts_dir": str(artifacts_dir),
        "goal_file": str(goal_file),
        "goal_preview": goal_preview[:4000],
        "command": cmd,
        "effective_max_iterations": (
            session_summary.get("estimated_iterations")
            if isinstance(session_summary, dict)
            else arguments.get("max_iterations")
        ),
        "session_summary_path": str(session_summary_path),
        "plan_path": str(artifact_path(artifacts_dir, "plan")),
        "result_path": str(artifact_path(artifacts_dir, "result")),
        "verdict_path": str(artifact_path(artifacts_dir, "verdict")),
        "stdout": stdout,
        "session_summary": session_summary,
    }


def run_dir(run_id: str) -> Path:
    return RUNS_ROOT / run_id


def run_metadata_path(run_id: str) -> Path:
    return run_dir(run_id) / "run.json"


def latest_log_excerpt(log_path: Path, max_lines: int = 12) -> str:
    if not log_path.exists():
        return ""
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def infer_progress(metadata: dict[str, Any]) -> dict[str, Any]:
    log_path = Path(metadata["log_path"])
    excerpt = latest_log_excerpt(log_path)
    lines = excerpt.splitlines()
    current_stage = "queued"
    current_iteration = None
    progress_message = "Job queued."
    total_iterations = metadata.get("requested_max_iterations")
    key_results: list[str] = []

    for line in lines:
        if "auto-estimated max_iterations=" in line or "loop budget:" in line:
            current_stage = "planning"
            progress_message = line.strip()
            if "max_iterations=" in line:
                fragment = line.split("max_iterations=", 1)[1].split(" ", 1)[0]
                total_iterations = int(fragment) if fragment.isdigit() else total_iterations
            elif "loop budget:" in line:
                fragment = line.split("loop budget:", 1)[1].strip().split(" ", 1)[0]
                total_iterations = int(fragment) if fragment.isdigit() else total_iterations
        if "== develop(iteration " in line:
            current_stage = "develop"
            fragment = line.split("== develop(iteration ", 1)[1].split(")", 1)[0]
            current_iteration = int(fragment) if fragment.isdigit() else current_iteration
            progress_message = line.strip()
        if "== verify(iteration " in line:
            current_stage = "verify"
            fragment = line.split("== verify(iteration ", 1)[1].split(")", 1)[0]
            current_iteration = int(fragment) if fragment.isdigit() else current_iteration
            progress_message = line.strip()
        if "artifact:" in line and current_stage == "queued":
            current_stage = "planning"
            progress_message = line.strip()
        if "exit_code=" in line:
            progress_message = line.strip()

    status = metadata.get("status", "unknown")
    session_summary = metadata.get("session_summary")
    if isinstance(session_summary, dict):
        estimated = session_summary.get("estimated_iterations")
        if isinstance(estimated, int):
            total_iterations = estimated
        iterations = session_summary.get("iterations")
        if isinstance(iterations, list) and iterations:
            latest = iterations[-1]
            if isinstance(latest, dict):
                if current_iteration is None and isinstance(latest.get("iteration"), int):
                    current_iteration = latest["iteration"]
                verdict_status = latest.get("verdict_status")
                result_status = latest.get("result_status")
                completed = latest.get("completed_task_ids", [])
                if isinstance(verdict_status, str):
                    key_results.append(f"latest verdict: {verdict_status}")
                if isinstance(result_status, str):
                    key_results.append(f"latest result: {result_status}")
                if isinstance(completed, list) and completed:
                    key_results.append("completed tasks: " + ", ".join(str(item) for item in completed[:5]))

    if status == "completed":
        current_stage = "done"
        pipeline_status = metadata.get("pipeline_status")
        if isinstance(pipeline_status, str):
            progress_message = f"Pipeline completed with status={pipeline_status}."
        else:
            progress_message = "Pipeline completed."
    elif status == "failed":
        current_stage = "failed"
        exit_code = metadata.get("exit_code")
        progress_message = f"Pipeline failed with exit_code={exit_code}."
    elif status == "running" and current_stage == "queued":
        current_stage = "running"
        progress_message = "Pipeline is running. Waiting for the first progress update."

    return {
        "total_iterations": total_iterations,
        "current_stage": current_stage,
        "current_iteration": current_iteration,
        "progress_message": progress_message,
        "key_results": key_results,
        "recent_log_excerpt": excerpt,
    }


def start_pipeline_run(arguments: dict[str, Any]) -> dict[str, Any]:
    repo, artifacts_dir, goal_file, goal_preview = prepare_run_inputs(arguments)
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    current_run_dir = run_dir(run_id)
    current_run_dir.mkdir(parents=True, exist_ok=False)
    log_path = current_run_dir / "run.log"
    metadata_path = run_metadata_path(run_id)
    metadata = {
        "run_id": run_id,
        "status": "queued",
        "repo": str(repo),
        "artifacts_dir": str(artifacts_dir),
        "goal_file": str(goal_file),
        "goal_preview": goal_preview[:4000],
        "session_summary_path": str(artifact_path(artifacts_dir, "session_summary")),
        "plan_path": str(artifact_path(artifacts_dir, "plan")),
        "result_path": str(artifact_path(artifacts_dir, "result")),
        "verdict_path": str(artifact_path(artifacts_dir, "verdict")),
        "requested_max_iterations": arguments.get("max_iterations"),
        "log_path": str(log_path),
        "run_metadata_path": str(metadata_path),
    }
    write_json(metadata_path, metadata)

    worker_cmd = [
        sys.executable,
        str(ROOT / "dual_pipeline" / "run_worker.py"),
        "--run-metadata",
        str(metadata_path),
        "--log-path",
        str(log_path),
        "--repo",
        str(repo),
        "--goal-file",
        str(goal_file),
        "--artifacts-dir",
        str(artifacts_dir),
    ]
    if arguments.get("max_iterations") is not None:
        worker_cmd.extend(["--max-iterations", str(int(arguments["max_iterations"]))])
    if arguments.get("base"):
        worker_cmd.extend(["--base", str(arguments["base"])])
    if arguments.get("model"):
        worker_cmd.extend(["--model", str(arguments["model"])])
    if bool(arguments.get("dry_run", False)):
        worker_cmd.append("--dry-run")

    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            worker_cmd,
            cwd=str(ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
            env=os.environ.copy(),
        )

    metadata["status"] = "running"
    metadata["pid"] = process.pid
    write_json(metadata_path, metadata)
    return {
        "run_id": run_id,
        "status": metadata["status"],
        "repo": metadata["repo"],
        "artifacts_dir": metadata["artifacts_dir"],
        "goal_file": metadata["goal_file"],
        "goal_preview": metadata["goal_preview"],
        "log_path": metadata["log_path"],
        "run_metadata_path": metadata["run_metadata_path"],
        "pid": process.pid,
    }


def get_pipeline_run(arguments: dict[str, Any]) -> dict[str, Any]:
    run_id = str(arguments["run_id"])
    metadata_path = run_metadata_path(run_id)
    if not metadata_path.exists():
        raise ValueError(f"unknown run_id: {run_id}")
    metadata = load_json(metadata_path)
    session_summary = read_json_if_exists(Path(metadata["session_summary_path"]))
    if session_summary is not None:
        metadata["session_summary"] = session_summary
        if metadata.get("status") == "running":
            metadata["pipeline_status"] = session_summary.get("status")
    metadata.update(infer_progress(metadata))
    write_json(metadata_path, metadata)
    return metadata


def tail_pipeline_log(arguments: dict[str, Any]) -> dict[str, Any]:
    run_id = str(arguments["run_id"])
    max_lines = int(arguments.get("max_lines", 80))
    metadata_path = run_metadata_path(run_id)
    if not metadata_path.exists():
        raise ValueError(f"unknown run_id: {run_id}")
    metadata = load_json(metadata_path)
    log_path = Path(metadata["log_path"])
    if not log_path.exists():
        content = ""
    else:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        content = "\n".join(lines[-max_lines:])
    return {
        "run_id": run_id,
        "log_path": str(log_path),
        "content": content,
    }


def read_pipeline_artifact(arguments: dict[str, Any]) -> dict[str, Any]:
    artifacts_dir = Path(str(arguments["artifacts_dir"])).expanduser().resolve()
    name = str(arguments["name"])
    path = artifact_path(artifacts_dir, name)
    exists = path.exists()
    content = read_json_if_exists(path) if exists else None
    return {
        "path": str(path),
        "exists": exists,
        "name": name,
        "content": content,
    }


async def async_main() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-dual-pipeline-mcp",
        description="Expose the local pipeline loop as an MCP stdio server.",
    )
    return parser.parse_args()


def main() -> None:
    parse_args()
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
