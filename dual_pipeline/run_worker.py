from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(prog="aicoding-dual-pipeline-run-worker")
    parser.add_argument("--run-metadata", required=True)
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--goal-file", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--base")
    parser.add_argument("--model")
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    metadata_path = Path(args.run_metadata).resolve()
    log_path = Path(args.log_path).resolve()
    metadata = load_json(metadata_path)
    metadata["status"] = "running"
    metadata["started_at"] = now_iso()
    write_json(metadata_path, metadata)

    cmd = [
        sys.executable,
        str(ROOT / "dual_pipeline" / "cli.py"),
        "--repo",
        args.repo,
        "--goal-file",
        args.goal_file,
        "--artifacts-dir",
        args.artifacts_dir,
    ]
    if args.max_iterations is not None:
        cmd.extend(["--max-iterations", str(args.max_iterations)])
    if args.base:
        cmd.extend(["--base", args.base])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.dry_run:
        cmd.append("--dry-run")
    cmd.append("loop")

    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{now_iso()}] starting command: {' '.join(cmd)}\n")
        log_file.flush()
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        log_file.write(f"[{now_iso()}] exit_code={result.returncode}\n")
        log_file.flush()

    metadata = load_json(metadata_path)
    session_summary_path = Path(metadata["session_summary_path"])
    session_summary = None
    if session_summary_path.exists():
        session_summary = load_json(session_summary_path)

    if result.returncode == 0:
        metadata["status"] = "completed"
        if isinstance(session_summary, dict) and isinstance(session_summary.get("status"), str):
            metadata["pipeline_status"] = session_summary["status"]
    else:
        metadata["status"] = "failed"
    metadata["completed_at"] = now_iso()
    metadata["exit_code"] = result.returncode
    metadata["session_summary"] = session_summary
    write_json(metadata_path, metadata)


if __name__ == "__main__":
    main()
