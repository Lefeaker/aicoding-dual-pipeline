from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas"
OUTPUT_POLL_INTERVAL_SECONDS = 1.0
PROCESS_EXIT_GRACE_SECONDS = 15.0
MAX_STAGE_WAIT_SECONDS = 60.0 * 60.0 * 6.0


@dataclass
class StageConfig:
    name: str
    schema_path: Path
    sandbox: str
    prompt: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codex-dual-pipeline",
        description="Run a reviewer/developer/verifier Codex pipeline against a local git repo.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Absolute or relative path to the target git repository.",
    )
    parser.add_argument(
        "--artifacts-dir",
        default="artifacts",
        help="Directory used to store plan/result/verdict JSON files.",
    )
    parser.add_argument(
        "--goal",
        help="Short natural-language goal for this run.",
    )
    parser.add_argument(
        "--goal-file",
        help="Read the goal from a text or markdown file.",
    )
    parser.add_argument(
        "--base",
        help="Optional base branch for diff context, e.g. origin/main.",
    )
    parser.add_argument(
        "--model",
        help="Optional Codex model override.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the codex commands that would run without invoking them.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        help="Optional override for reviewer/developer loop iterations for the loop command.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("review", help="Generate a structured plan.json from repo context.")
    subparsers.add_parser("develop", help="Execute plan.json and emit result.json.")
    subparsers.add_parser("verify", help="Review the implementation result and emit verdict.json.")
    subparsers.add_parser("run", help="Run review, develop, and verify in sequence.")
    subparsers.add_parser("loop", help="Run initial review, then loop develop/verify until done.")
    return parser.parse_args()


def read_goal(args: argparse.Namespace) -> str:
    if args.goal_file:
        return Path(args.goal_file).read_text(encoding="utf-8").strip()
    if args.goal:
        return args.goal.strip()
    raise SystemExit("one of --goal or --goal-file is required")


def ensure_repo(path_str: str) -> Path:
    repo = Path(path_str).expanduser().resolve()
    if not repo.exists():
        raise SystemExit(f"repo does not exist: {repo}")
    if not (repo / ".git").exists():
        raise SystemExit(f"repo is not a git repository: {repo}")
    return repo


def run_cmd(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "(no stderr)"
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}\n{stderr}")
    return result.stdout.strip()


def maybe_run_codex(cmd: list[str], dry_run: bool, output_path: Path | None = None) -> None:
    printable = " ".join(shlex.quote(part) for part in cmd)
    print(printable)
    if dry_run:
        return
    process = subprocess.Popen(cmd)
    start = time.monotonic()
    output_ready_at: float | None = None

    while True:
        return_code = process.poll()
        output_ready = is_valid_output_file(output_path)

        if output_ready and output_ready_at is None:
            output_ready_at = time.monotonic()

        if return_code is not None:
            if return_code != 0:
                raise SystemExit(return_code)
            return

        if output_ready and output_ready_at is not None:
            waited_since_output = time.monotonic() - output_ready_at
            if waited_since_output >= PROCESS_EXIT_GRACE_SECONDS:
                print(
                    f"warning: codex exec wrote {output_path} but did not exit after "
                    f"{PROCESS_EXIT_GRACE_SECONDS:.0f}s; terminating hung process"
                )
                terminate_process(process)
                return

        if time.monotonic() - start >= MAX_STAGE_WAIT_SECONDS:
            terminate_process(process)
            raise SystemExit(f"codex stage timed out after {MAX_STAGE_WAIT_SECONDS:.0f}s: {' '.join(cmd)}")

        time.sleep(OUTPUT_POLL_INTERVAL_SECONDS)


def is_valid_output_file(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return True


def terminate_process(process: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass


def collect_repo_context(repo: Path, base: str | None) -> dict[str, str]:
    branch = run_cmd(["git", "branch", "--show-current"], repo)
    status = run_cmd(["git", "status", "--short"], repo)
    staged_stat = run_cmd(["git", "diff", "--cached", "--stat"], repo)
    unstaged_stat = run_cmd(["git", "diff", "--stat"], repo)
    recent_commits = run_cmd(["git", "log", "--oneline", "-5"], repo)

    context = {
        "branch": branch or "(detached)",
        "status": status or "(clean)",
        "staged_stat": staged_stat or "(no staged changes)",
        "unstaged_stat": unstaged_stat or "(no unstaged changes)",
        "recent_commits": recent_commits or "(no commits)",
    }
    if base:
        context["base"] = base
        try:
            context["merge_base"] = run_cmd(["git", "merge-base", "HEAD", base], repo)
            context["base_diff_stat"] = run_cmd(["git", "diff", "--stat", f"{base}...HEAD"], repo) or "(no diff)"
        except RuntimeError as exc:
            context["base_error"] = str(exc)
    return context


def build_reviewer_prompt(goal: str, repo: Path, context: dict[str, str]) -> str:
    return textwrap.dedent(
        f"""
        You are the Reviewer / Planner in a strict dual-role software delivery pipeline.
        Your responsibilities are limited to:
        - inspect repository state
        - identify code-review findings or task opportunities
        - decompose work into concrete tasks
        - define acceptance criteria and constraints
        - decide whether implementation should proceed

        You must not propose that you directly edit code.
        Return JSON only that matches the provided schema.

        Goal:
        {goal}

        Repository:
        {repo}

        Current context:
        {json.dumps(context, ensure_ascii=False, indent=2)}

        Guidance:
        - Produce tasks that a separate Developer / Executor can follow mechanically.
        - Prefer explicit file paths when you can infer them from the repository state.
        - Keep acceptance criteria testable.
        - If the repo state is already sufficient, you may return an empty task list with proceed=false.
        """
    ).strip()


def build_developer_prompt(goal: str, repo: Path, plan: dict[str, object]) -> str:
    return textwrap.dedent(
        f"""
        You are the Developer / Executor in a strict dual-role software delivery pipeline.
        Your responsibilities are limited to:
        - read the structured plan
        - modify code to satisfy the tasks
        - run targeted validation commands when appropriate
        - report implementation status, changed files, validations, blockers, and follow-up work

        You must not redefine the plan unless a blocker forces it.
        You are allowed to edit files in the repository.
        Return JSON only that matches the provided schema.

        Goal:
        {goal}

        Repository:
        {repo}

        Approved plan:
        {json.dumps(plan, ensure_ascii=False, indent=2)}

        Guidance:
        - Implement the plan as written.
        - Keep changes scoped.
        - Run the smallest useful validation set and report exact commands.
        - If blocked, explain precisely what prevented completion.
        """
    ).strip()


def build_verifier_prompt(
    goal: str,
    repo: Path,
    plan: dict[str, object],
    result: dict[str, object],
    context: dict[str, str],
) -> str:
    return textwrap.dedent(
        f"""
        You are the Reviewer / Verifier in a strict dual-role software delivery pipeline.
        Your responsibilities are limited to:
        - inspect the latest repository state
        - compare implementation results against the original plan
        - decide pass, needs_revision, or blocked
        - produce focused follow-up tasks if required

        You must not edit code.
        Return JSON only that matches the provided schema.

        Goal:
        {goal}

        Repository:
        {repo}

        Original plan:
        {json.dumps(plan, ensure_ascii=False, indent=2)}

        Developer result:
        {json.dumps(result, ensure_ascii=False, indent=2)}

        Current repo context:
        {json.dumps(context, ensure_ascii=False, indent=2)}

        Guidance:
        - Be strict about acceptance criteria.
        - If verification fails, produce concise follow-up tasks that the developer can execute next.
        - Prefer concrete evidence from the current repository state.
        """
    ).strip()


def build_iteration_budget_prompt(goal: str, repo: Path, context: dict[str, str]) -> str:
    return textwrap.dedent(
        f"""
        You are estimating the iteration budget for a reviewer/developer delivery loop.
        Read the task brief and current repository context, then choose a conservative loop budget.
        Return JSON only that matches the provided schema.

        Goal:
        {goal}

        Repository:
        {repo}

        Current context:
        {json.dumps(context, ensure_ascii=False, indent=2)}

        Guidance:
        - Estimate how many reviewer/developer cycles are likely needed.
        - Use a value between 2 and 12.
        - Always include explicit slack instead of using a tight minimum.
        - Prefer a higher budget when the task appears broad, risky, integration-heavy, or test-heavy.
        - It is better to slightly overestimate than to cut the loop budget too close.
        - Include a short rationale.
        """
    ).strip()


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def placeholder_plan(goal: str) -> dict[str, object]:
    return {
        "goal": goal,
        "proceed": True,
        "summary": "Dry-run placeholder plan.",
        "tasks": [
            {
                "id": "DRYRUN-T1",
                "title": "Placeholder task for dry-run command generation",
                "priority": "medium",
                "files": [],
                "changes": ["Replace this placeholder by running the review stage for real."],
                "acceptance": ["Generate a real plan.json before executing develop for real."]
            }
        ],
        "constraints": [],
        "risks": ["Dry-run placeholder; not suitable for real execution."]
    }


def placeholder_result() -> dict[str, object]:
    return {
        "status": "partial",
        "summary": "Dry-run placeholder result.",
        "completed_task_ids": [],
        "changed_files": [],
        "validations": [
            {
                "command": "(not run)",
                "status": "not_run",
                "details": "Dry-run mode only."
            }
        ],
        "blockers": ["Dry-run placeholder; no real implementation occurred."],
        "follow_up": ["Run the develop stage without --dry-run to generate a real result.json."]
    }


def prepare_artifact_dir(path_str: str) -> Path:
    path = Path(path_str).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_codex_command(
    *,
    stage: StageConfig,
    repo: Path,
    output_path: Path,
    model: str | None,
) -> list[str]:
    cmd = [
        "codex",
        "exec",
        "--cd",
        str(repo),
        "--sandbox",
        stage.sandbox,
        "--output-schema",
        str(stage.schema_path),
        "--output-last-message",
        str(output_path),
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append(stage.prompt)
    return cmd


def heuristic_iteration_budget(goal: str) -> dict[str, object]:
    text = goal.lower()
    bullets = goal.count("\n- ")
    checks = sum(text.count(token) for token in ["测试", "test", "lint", "build", "refactor", "迁移", "migration"])
    risk = sum(text.count(token) for token in ["不要", "不能", "兼容", "rollback", "回滚", "public api"])
    estimate = 2 + min(6, bullets // 2) + min(2, checks) + min(2, risk)
    loops = max(2, min(12, estimate + 1))
    return {
        "estimated_iterations": loops,
        "rationale": "Heuristic fallback with built-in slack based on task breadth, validation load, and constraints.",
    }


def apply_iteration_slack(estimated_iterations: int, rationale: str) -> dict[str, object]:
    buffered = min(12, max(2, estimated_iterations + 1))
    if buffered == estimated_iterations:
        final_rationale = f"{rationale} Slack policy applied, but capped at the allowed maximum."
    else:
        final_rationale = f"{rationale} Added one extra loop as default slack."
    return {
        "estimated_iterations": buffered,
        "rationale": final_rationale,
    }


def estimate_iteration_budget(args: argparse.Namespace, repo: Path, artifacts: Path, goal: str) -> dict[str, object]:
    if args.max_iterations is not None:
        return {
            "estimated_iterations": args.max_iterations,
            "rationale": "Explicit max_iterations override from caller.",
        }

    if args.dry_run:
        estimate = heuristic_iteration_budget(goal)
        print(
            f"auto-estimated max_iterations={estimate['estimated_iterations']} "
            f"(dry-run heuristic: {estimate['rationale']})"
        )
        return estimate

    context = collect_repo_context(repo, args.base)
    prompt = build_iteration_budget_prompt(goal, repo, context)
    output_path = artifacts / "iteration_budget.json"
    stage = StageConfig(
        name="estimate",
        schema_path=SCHEMA_DIR / "iteration-budget.schema.json",
        sandbox="read-only",
        prompt=prompt,
    )
    maybe_run_codex(
        build_codex_command(stage=stage, repo=repo, output_path=output_path, model=args.model),
        args.dry_run,
        output_path,
    )
    if output_path.exists():
        data = load_json(output_path)
        raw_value = data.get("estimated_iterations")
        if isinstance(raw_value, int):
            buffered = apply_iteration_slack(
                max(2, min(12, raw_value)),
                str(data.get("rationale", "Model-estimated iteration budget.")),
            )
            return buffered
    return heuristic_iteration_budget(goal)


def do_review(args: argparse.Namespace, repo: Path, artifacts: Path, goal: str) -> Path:
    context = collect_repo_context(repo, args.base)
    prompt = build_reviewer_prompt(goal, repo, context)
    output_path = artifacts / "plan.json"
    stage = StageConfig(
        name="review",
        schema_path=SCHEMA_DIR / "plan.schema.json",
        sandbox="read-only",
        prompt=prompt,
    )
    maybe_run_codex(
        build_codex_command(stage=stage, repo=repo, output_path=output_path, model=args.model),
        args.dry_run,
        output_path,
    )
    return output_path


def do_develop(args: argparse.Namespace, repo: Path, artifacts: Path, goal: str) -> Path:
    plan_path = artifacts / "plan.json"
    if not plan_path.exists() and not args.dry_run:
        raise SystemExit(f"missing plan file: {plan_path}")
    plan = load_json(plan_path) if plan_path.exists() else placeholder_plan(goal)
    prompt = build_developer_prompt(goal, repo, plan)
    output_path = artifacts / "result.json"
    stage = StageConfig(
        name="develop",
        schema_path=SCHEMA_DIR / "result.schema.json",
        sandbox="workspace-write",
        prompt=prompt,
    )
    maybe_run_codex(
        build_codex_command(stage=stage, repo=repo, output_path=output_path, model=args.model),
        args.dry_run,
        output_path,
    )
    return output_path


def do_verify(args: argparse.Namespace, repo: Path, artifacts: Path, goal: str) -> Path:
    plan_path = artifacts / "plan.json"
    result_path = artifacts / "result.json"
    if not plan_path.exists() and not args.dry_run:
        raise SystemExit(f"missing plan file: {plan_path}")
    if not result_path.exists() and not args.dry_run:
        raise SystemExit(f"missing result file: {result_path}")
    plan = load_json(plan_path) if plan_path.exists() else placeholder_plan(goal)
    result = load_json(result_path) if result_path.exists() else placeholder_result()
    context = collect_repo_context(repo, args.base)
    prompt = build_verifier_prompt(goal, repo, plan, result, context)
    output_path = artifacts / "verdict.json"
    stage = StageConfig(
        name="verify",
        schema_path=SCHEMA_DIR / "verdict.schema.json",
        sandbox="read-only",
        prompt=prompt,
    )
    maybe_run_codex(
        build_codex_command(stage=stage, repo=repo, output_path=output_path, model=args.model),
        args.dry_run,
        output_path,
    )
    return output_path


def print_artifact_summary(path: Path) -> None:
    print(f"artifact: {path}")
    if path.exists():
        try:
            data = load_json(path)
        except json.JSONDecodeError:
            print("warning: artifact is not valid JSON")
            return
        print(json.dumps(data, ensure_ascii=False, indent=2))


def iteration_plan_from_verdict(
    goal: str,
    prior_plan: dict[str, object],
    verdict: dict[str, object],
    iteration: int,
) -> dict[str, object]:
    raw_next_tasks = verdict.get("next_tasks", [])
    tasks: list[dict[str, object]] = []
    if isinstance(raw_next_tasks, list):
        for index, raw_task in enumerate(raw_next_tasks, start=1):
            if not isinstance(raw_task, dict):
                continue
            title = str(raw_task.get("title", "Follow-up task"))
            acceptance = raw_task.get("acceptance", [])
            tasks.append(
                {
                    "id": str(raw_task.get("id", f"LOOP-{iteration}-{index}")),
                    "title": title,
                    "priority": "high",
                    "files": [],
                    "changes": [title],
                    "acceptance": acceptance if isinstance(acceptance, list) else [],
                }
            )

    constraints = prior_plan.get("constraints", [])
    risks = prior_plan.get("risks", [])
    findings = verdict.get("findings", [])

    return {
        "goal": goal,
        "proceed": True,
        "summary": str(verdict.get("summary", f"Follow-up plan for iteration {iteration}")),
        "tasks": tasks,
        "constraints": constraints if isinstance(constraints, list) else [],
        "risks": (risks if isinstance(risks, list) else []) + (findings if isinstance(findings, list) else []),
    }


def do_loop(args: argparse.Namespace, repo: Path, artifacts: Path, goal: str) -> Path:
    budget = estimate_iteration_budget(args, repo, artifacts, goal)
    max_iterations = int(budget["estimated_iterations"])
    print(f"loop budget: {max_iterations} ({budget.get('rationale', 'no rationale')})")
    do_review(args, repo, artifacts, goal)

    if args.dry_run:
        for iteration in range(1, max_iterations + 1):
            print(f"== develop(iteration {iteration}) ==")
            do_develop(args, repo, artifacts, goal)
            print(f"== verify(iteration {iteration}) ==")
            do_verify(args, repo, artifacts, goal)
            break
        return artifacts / "session_summary.json"

    current_plan = load_json(artifacts / "plan.json")
    summary: dict[str, object] = {
        "goal": goal,
        "repo": str(repo),
        "status": "running",
        "estimated_iterations": max_iterations,
        "iteration_budget_rationale": budget.get("rationale"),
        "iterations": [],
        "final_verdict": None,
    }

    if not current_plan.get("proceed", True):
        summary["status"] = "no_work"
        write_json(artifacts / "session_summary.json", summary)
        return artifacts / "session_summary.json"

    for iteration in range(1, max_iterations + 1):
        print(f"== develop(iteration {iteration}) ==")
        result_path = do_develop(args, repo, artifacts, goal)
        result = load_json(result_path)

        print(f"== verify(iteration {iteration}) ==")
        verdict_path = do_verify(args, repo, artifacts, goal)
        verdict = load_json(verdict_path)

        record = {
            "iteration": iteration,
            "result_status": result.get("status"),
            "verdict_status": verdict.get("status"),
            "completed_task_ids": result.get("completed_task_ids", []),
            "accepted_task_ids": verdict.get("accepted_task_ids", []),
            "rejected_task_ids": verdict.get("rejected_task_ids", []),
        }
        iterations = summary["iterations"]
        assert isinstance(iterations, list)
        iterations.append(record)

        verdict_status = verdict.get("status")
        if verdict_status == "pass":
            summary["status"] = "completed"
            summary["final_verdict"] = verdict
            write_json(artifacts / "session_summary.json", summary)
            return artifacts / "session_summary.json"

        if verdict_status == "blocked":
            summary["status"] = "blocked"
            summary["final_verdict"] = verdict
            write_json(artifacts / "session_summary.json", summary)
            return artifacts / "session_summary.json"

        current_plan = iteration_plan_from_verdict(goal, current_plan, verdict, iteration + 1)
        if not current_plan["tasks"]:
            summary["status"] = "stalled"
            summary["final_verdict"] = verdict
            write_json(artifacts / "session_summary.json", summary)
            return artifacts / "session_summary.json"

        write_json(artifacts / "plan.json", current_plan)
        write_json(artifacts / f"plan.iteration-{iteration + 1}.json", current_plan)

    summary["status"] = "max_iterations_reached"
    summary["final_verdict"] = load_json(artifacts / "verdict.json") if (artifacts / "verdict.json").exists() else None
    write_json(artifacts / "session_summary.json", summary)
    return artifacts / "session_summary.json"


def main() -> None:
    args = parse_args()
    repo = ensure_repo(args.repo)
    artifacts = prepare_artifact_dir(args.artifacts_dir)
    goal = read_goal(args)

    commands = {
        "review": lambda: do_review(args, repo, artifacts, goal),
        "develop": lambda: do_develop(args, repo, artifacts, goal),
        "verify": lambda: do_verify(args, repo, artifacts, goal),
        "loop": lambda: do_loop(args, repo, artifacts, goal),
    }

    if args.command == "run":
        for name in ("review", "develop", "verify"):
            print(f"== {name} ==")
            artifact = commands[name]()
            if not args.dry_run:
                print_artifact_summary(artifact)
        return

    artifact = commands[args.command]()
    if not args.dry_run and artifact.exists():
        print_artifact_summary(artifact)


if __name__ == "__main__":
    main()
