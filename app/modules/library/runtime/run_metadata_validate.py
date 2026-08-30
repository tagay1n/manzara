"""Audit persisted Library metadata and queue invalid rows for re-extraction."""

from __future__ import annotations

import argparse
import os
import signal
from typing import Any, Mapping

from app.db import Database
from app.modules.library.metadata_quality import MetadataQualityRepository
from app.run_artifact_channel import emit_run_artifact
from app.settings import load_settings


TASK_ID = "library.metadata_validate"
PANEL_ID = "metadata"


def _run_id(required: bool) -> int | None:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if value.isdigit() and int(value) > 0:
        return int(value)
    if required:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required unless --dry-run is used")
    return None


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    return parser.parse_args()


def main() -> int:
    args = _args()
    run_id = _run_id(required=not args.dry_run)
    settings = load_settings()
    repository = MetadataQualityRepository(
        settings.database_url, schema=settings.database_schema
    )
    db = (
        Database(settings.database_url, schema=settings.database_schema)
        if run_id
        else None
    )
    stop = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop["requested"] = True
        print("metadata validation: graceful stop requested", flush=True)

    def publish(current: int, total: int, counters: Mapping[str, int]) -> None:
        if db is None or run_id is None:
            return
        progress = {
            "current": current,
            "total": total,
            "percent": round((current / total) * 100, 2) if total else 100,
            "invalid": int(counters.get("invalid") or 0),
            "resolved": int(counters.get("resolved") or 0),
            "normalized": int(counters.get("normalized") or 0),
        }
        db.update_run_progress(run_id, progress)
        db.insert_event(
            "task.progress",
            task_id=TASK_ID,
            run_id=run_id,
            panel_id=PANEL_ID,
            payload={"status": "running", "progress": progress},
        )

    signal.signal(signal.SIGINT, request_stop)
    try:
        summary = repository.audit(
            apply=not args.dry_run,
            run_id=run_id,
            batch_size=args.batch_size,
            should_stop=lambda: stop["requested"],
            on_progress=publish,
        )
        if run_id is not None:
            emit_run_artifact(summary)
        else:
            print(summary, flush=True)
        return 0
    finally:
        repository.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
