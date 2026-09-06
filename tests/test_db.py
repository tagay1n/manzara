"""Database-layer tests for run recovery behavior."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text

from app.db import Database


@contextmanager
def _isolated_database(database_url: str) -> Database:
    schema_name = f"manzara_test_{uuid.uuid4().hex[:10]}"
    db = Database(database_url, schema=schema_name)
    db.init_schema()
    try:
        yield db
    finally:
        db.close()
        engine = create_engine(database_url)
        try:
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        finally:
            engine.dispose()


def test_recover_active_runs_marks_running_as_failed(
    tmp_path: Path, test_database_url: str
) -> None:
    with _isolated_database(test_database_url) as db:
        db.seed_tasks(
            [
                {
                    "task_id": "t1",
                    "panel_id": "maintenance",
                    "title": "Task",
                    "task_type": "scan",
                    "icon_idle": "Play",
                    "icon_running": "Square",
                    "command": {"mode": "shell", "value": "echo hi"},
                    "cwd": str(tmp_path),
                }
            ]
        )

        run_id = db.create_run(db.get_task("t1"))
        db.mark_run_started(run_id, pid=12345)

        recovered = db.recover_active_runs()
        assert recovered == 1

        run = db.get_run(run_id)
        assert run["status"] == "failed"
        assert run["finished_at"] is not None
        assert "Recovered after Manzara restart" in (run["error_text"] or "")


def test_run_progress_round_trip(tmp_path: Path, test_database_url: str) -> None:
    with _isolated_database(test_database_url) as db:
        db.seed_tasks(
            [
                {
                    "task_id": "maintenance.progress_test",
                    "panel_id": "maintenance",
                    "title": "Copy videos",
                    "task_type": "transfer",
                    "icon_idle": "CloudCog",
                    "icon_running": "Square",
                    "command": {"mode": "shell", "value": "echo hi"},
                    "cwd": str(tmp_path),
                }
            ]
        )
        run_id = db.create_run(db.get_task("maintenance.progress_test"))
        progress = {"current": 2, "total": 8, "percent": 25}
        db.update_run_progress(run_id, progress)
        assert db.get_run(run_id)["progress"] == progress
        assert (
            db.list_recent_runs_for_task("maintenance.progress_test")[0]["progress"]
            == progress
        )


def test_run_progress_is_coalesced_and_removed_from_event_history(
    tmp_path: Path, test_database_url: str
) -> None:
    with _isolated_database(test_database_url) as db:
        task = {
            "task_id": "maintenance.coalesced_progress",
            "panel_id": "maintenance",
            "title": "Progress",
            "task_type": "test",
            "icon_idle": "Play",
            "icon_running": "Square",
            "command": {"mode": "shell", "value": "echo hi"},
            "cwd": str(tmp_path),
        }
        db.seed_tasks([task])
        run_id = db.create_run(db.get_task(task["task_id"]))

        assert db.publish_run_progress(
            run_id=run_id,
            task_id=task["task_id"],
            panel_id=task["panel_id"],
            progress={"current": 1},
        ) is True
        assert db.publish_run_progress(
            run_id=run_id,
            task_id=task["task_id"],
            panel_id=task["panel_id"],
            progress={"current": 2},
        ) is False
        assert db.get_run(run_id)["progress"] == {"current": 1}

        db.finish_run(run_id, "completed", 0, None)
        assert [
            event
            for event in db.get_events_after(0, limit=50)
            if int(event.get("run_id") or 0) == run_id
            and event.get("type") == "task.progress"
        ] == []


def test_prune_runtime_definitions_removes_stale_flow_rows(
    tmp_path: Path, test_database_url: str
) -> None:
    with _isolated_database(test_database_url) as db:
        db.seed_panels(
            [
                {"panel_id": "maintenance", "title": "Maintenance"},
                {"panel_id": "oscar", "title": "Oscar"},
            ]
        )
        db.seed_tasks(
            [
                {
                    "task_id": "maintenance.keep",
                    "panel_id": "maintenance",
                    "title": "Keep",
                    "task_type": "scan",
                    "icon_idle": "Play",
                    "icon_running": "Square",
                    "command": {"mode": "shell", "value": "echo keep"},
                    "cwd": str(tmp_path),
                },
                {
                    "task_id": "oscar.drop",
                    "panel_id": "oscar",
                    "title": "Drop",
                    "task_type": "ingest",
                    "icon_idle": "Play",
                    "icon_running": "Square",
                    "command": {"mode": "shell", "value": "echo drop"},
                    "cwd": str(tmp_path),
                },
            ]
        )

        stale_task = db.get_task("oscar.drop")
        assert stale_task is not None
        stale_run_id = db.create_run(stale_task)
        db.mark_run_started(stale_run_id, pid=123)
        db.finish_run(stale_run_id, status="failed", exit_code=1, error_text="boom")
        db.insert_event(
            "task.failed",
            task_id="oscar.drop",
            run_id=stale_run_id,
            panel_id="oscar",
            payload={"error": "boom"},
        )

        stats = db.prune_runtime_definitions(
            panel_ids=["maintenance"],
            task_ids=["maintenance.keep"],
        )
        assert stats["panels_removed"] >= 1
        assert stats["tasks_removed"] >= 1
        assert stats["runs_removed"] >= 1

        assert db.get_panel("oscar") is None
        assert db.get_task("oscar.drop") is None
        assert db.get_run(stale_run_id) is None

        assert db.get_panel("maintenance") is not None
        assert db.get_task("maintenance.keep") is not None
