"""Check whether pgBackRest backup objects are present in S3 for a backup run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List

from app.db import Database
from app.modules.maintenance.backup_s3_verify import verify_backup_objects_in_s3
from app.modules.maintenance.tasks import (
    MAINTENANCE_PGBACKREST_FULL_TASK_ID,
    MAINTENANCE_PGBACKREST_INCR_TASK_ID,
)
from app.settings import load_settings
from app.artifacts import task_runs_dir
from app.run_log_store import read_run_log


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify that backup files for a pgBackRest label are present in S3.",
    )
    parser.add_argument("--run-id", type=int, default=None, help="Manzara run_id to inspect.")
    parser.add_argument(
        "--task-id",
        type=str,
        default=MAINTENANCE_PGBACKREST_INCR_TASK_ID,
        choices=[MAINTENANCE_PGBACKREST_INCR_TASK_ID, MAINTENANCE_PGBACKREST_FULL_TASK_ID],
        help="Task id used to resolve latest completed run when --run-id is not provided.",
    )
    parser.add_argument("--label", type=str, default=None, help="Backup label override.")
    parser.add_argument("--bucket", type=str, default=None, help="S3 bucket override.")
    parser.add_argument("--stanza", type=str, default=None, help="pgBackRest stanza override.")
    parser.add_argument("--endpoint", type=str, default=None, help="S3 endpoint override.")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Explicit config yaml with backups.pgbackrest storage settings.",
    )
    parser.add_argument(
        "--monocorpus-repo",
        type=Path,
        default=Path("/home/tans1q/projects/monocorpus"),
        help="Monocorpus repo path for fallback config lookup.",
    )
    return parser


def resolve_run_id(db: Database, requested_run_id: int | None, task_id: str) -> int:
    if requested_run_id:
        return int(requested_run_id)
    recent = db.list_recent_runs_for_task(task_id, limit=120)
    for item in recent:
        if str(item.get("status") or "") == "completed":
            return int(item["run_id"])
    if recent:
        return int(recent[0]["run_id"])
    raise RuntimeError(f"No runs found for task_id={task_id}")


def load_run_logs(db: Database, run_id: int) -> List[str]:
    run = db.get_run(run_id)
    if not run:
        raise RuntimeError(f"Run not found: {run_id}")
    rows = read_run_log(
        task_runs_dir(),
        str(run.get("task_id") or ""),
        run_id,
        after_log_id=0,
        limit=8000,
    )
    return [str(row.get("line") or "") for row in rows]


def main() -> int:
    args = build_parser().parse_args()
    settings = load_settings()
    db = Database(settings.database_url, schema=settings.database_schema)
    run_id = resolve_run_id(db, args.run_id, args.task_id)
    log_lines = load_run_logs(db, run_id)

    result = verify_backup_objects_in_s3(
        log_lines,
        label=args.label,
        bucket=args.bucket,
        stanza=args.stanza,
        endpoint=args.endpoint,
        config_path=args.config,
        monocorpus_repo_path=args.monocorpus_repo,
    )
    result["run_id"] = run_id
    result["task_id"] = args.task_id
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
