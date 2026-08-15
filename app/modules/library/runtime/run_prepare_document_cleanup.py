"""Prepare document cleanup plans without mutating documents or storage."""

from __future__ import annotations

import os
import signal
from typing import Any, Mapping

from app.db import Database
from app.document_storage import load_document_storage_settings
from app.modules.library.document_cleanup_repository import DocumentCleanupRepository
from app.modules.library.document_cleanup_service import prepare_document_cleanup
from app.run_artifact_channel import emit_run_artifact
from app.runtime_config import load_runtime_config
from app.settings import load_settings


TASK_ID = "library.prepare_document_cleanup"
PANEL_ID = "maintenance"


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


def _progress(current: int, total: int, counters: Mapping[str, int]) -> dict[str, Any]:
    return {
        "current": int(current),
        "total": int(total),
        "percent": round((current / total) * 100, 2) if total else 100,
        "plans_created": int(counters.get("plans_created") or 0),
        "planned_non_tatar": int(counters.get("planned_non_tatar") or 0),
        "planned_non_document": int(counters.get("planned_non_document") or 0),
        "planned_duplicate_isbn": int(counters.get("planned_duplicate_isbn") or 0),
        "reviews_created": int(counters.get("isbn_reviews_created") or 0),
    }


def main() -> int:
    run_id = _run_id()
    app_settings = load_settings()
    storage = load_document_storage_settings(load_runtime_config())
    repository = DocumentCleanupRepository(
        app_settings.database_url, schema=app_settings.database_schema
    )
    db = Database(app_settings.database_url, schema=app_settings.database_schema)
    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print("document cleanup preparation: graceful stop requested", flush=True)

    def publish(current: int, total: int, counters: Mapping[str, int]) -> None:
        payload = _progress(current, total, counters)
        db.update_run_progress(run_id, payload)
        db.insert_event(
            "task.progress",
            task_id=TASK_ID,
            run_id=run_id,
            panel_id=PANEL_ID,
            payload={"status": "running", "progress": payload},
        )

    signal.signal(signal.SIGINT, request_stop)
    try:
        print(f"document cleanup preparation: start run_id={run_id}", flush=True)
        summary = prepare_document_cleanup(
            repository=repository,
            filtered_out_path=storage.filtered_out_path,
            should_stop=lambda: bool(stop_state["requested"]),
            on_progress=publish,
        )
        emit_run_artifact(summary)
        return 0
    finally:
        repository.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
