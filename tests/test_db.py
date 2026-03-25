"""Database-layer tests for run recovery behavior."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import yaml
from sqlalchemy import create_engine, text

from app.db import Database


def _contains_redacted(node: object) -> bool:
    if isinstance(node, str):
        return "<REDACTED>" in node
    if isinstance(node, dict):
        return any(_contains_redacted(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_redacted(value) for value in node)
    return False


def _resolve_database_url() -> str:
    for env_name in ("MANZARA_TEST_DATABASE_URL", "MANZARA_DATABASE_URL"):
        value = str(os.environ.get(env_name) or "").strip()
        if value:
            return value

    for candidate in (Path("config.local.yaml"), Path("config.yaml"), Path("config.example.yaml")):
        if not candidate.exists():
            continue
        data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            continue
        if _contains_redacted(data):
            continue
        database_url = str(data.get("database_url") or "").strip()
        if database_url:
            return database_url

    raise RuntimeError("Tests require MANZARA_TEST_DATABASE_URL or an unmasked local config.")


@contextmanager
def _isolated_database() -> Database:
    database_url = _resolve_database_url()
    schema_name = f"manzara_test_{uuid.uuid4().hex[:10]}"
    db = Database(database_url, schema=schema_name)
    db.init_schema()
    try:
        yield db
    finally:
        engine = create_engine(database_url)
        try:
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        finally:
            engine.dispose()


def test_recover_active_runs_marks_running_as_failed(tmp_path: Path) -> None:
    with _isolated_database() as db:

        db.seed_tasks(
            [
                {
                    "task_id": "t1",
                    "panel_id": "shayan",
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


def test_recover_active_workflow_runs_marks_running_as_failed(tmp_path: Path) -> None:
    with _isolated_database() as db:

        db.seed_workflow_bundle(
            {
                "workflow": {
                    "workflow_id": "wf1",
                    "panel_id": "shayan",
                    "title": "WF",
                    "description": "test",
                    "enabled": 1,
                },
                "steps": [],
                "schedule": {
                    "schedule_id": "wf1.schedule",
                    "workflow_id": "wf1",
                    "schedule_type": "weekly",
                    "day_of_week": 1,
                    "time_of_day": "03:00",
                    "timezone": "UTC",
                    "enabled": 0,
                    "overlap_policy": "skip",
                    "catchup_policy": "once",
                },
            }
        )

        workflow_run_id = db.create_workflow_run(
            workflow_id="wf1",
            schedule_id=None,
            trigger_source="manual",
            context={},
        )
        db.update_workflow_run(workflow_run_id, status="running")

        recovered = db.recover_active_workflow_runs()
        assert recovered == 1

        run = db.get_workflow_run(workflow_run_id)
        assert run["status"] == "failed"
        assert run["finished_at"] is not None
        assert "Recovered after Manzara restart" in (run["error_text"] or "")


def test_oscar_snapshot_queue_claims_oldest_pending_first() -> None:
    with _isolated_database() as db:
        db.upsert_oscar_snapshot(
            "snap-b",
            source_path="/snapshots/b",
            discovered_at="2026-03-02T00:00:00+00:00",
        )
        db.upsert_oscar_snapshot(
            "snap-a",
            source_path="/snapshots/a",
            discovered_at="2026-03-01T00:00:00+00:00",
        )

        first = db.claim_next_oscar_snapshot()
        assert first is not None
        assert first["snapshot_id"] == "snap-a"
        assert first["status"] == "processing"

        second = db.claim_next_oscar_snapshot()
        assert second is not None
        assert second["snapshot_id"] == "snap-b"
        assert second["status"] == "processing"

        assert db.claim_next_oscar_snapshot() is None

        db.set_oscar_snapshot_status("snap-a", "completed")
        completed = db.list_oscar_snapshots(statuses=["completed"])
        assert any(row["snapshot_id"] == "snap-a" for row in completed)


def test_oscar_snapshot_stage_progress_upserts_by_snapshot_and_stage() -> None:
    with _isolated_database() as db:
        db.seed_tasks(
            [
                {
                    "task_id": "oscar.test_stage_task",
                    "panel_id": "oscar",
                    "title": "Oscar stage task",
                    "task_type": "extract",
                    "icon_idle": "Play",
                    "icon_running": "Square",
                    "command": {"mode": "shell", "value": "echo hi"},
                    "cwd": ".",
                }
            ]
        )
        task = db.get_task("oscar.test_stage_task")
        assert task is not None
        run_id_1 = db.create_run(task)
        run_id_2 = db.create_run(task)

        db.upsert_oscar_snapshot("snap-1", source_path="/snapshots/1")

        db.upsert_oscar_snapshot_stage(
            "snap-1",
            "resolve_offsets_local",
            "running",
            run_id=run_id_1,
        )
        db.upsert_oscar_snapshot_stage(
            "snap-1",
            "resolve_offsets_local",
            "completed",
            run_id=run_id_1,
        )
        db.upsert_oscar_snapshot_stage(
            "snap-1",
            "download_ranges",
            "failed",
            run_id=run_id_2,
            error_text="network timeout",
        )

        rows = db.list_oscar_snapshot_stages("snap-1")
        by_stage = {row["stage_name"]: row for row in rows}
        assert by_stage["resolve_offsets_local"]["status"] == "completed"
        assert by_stage["resolve_offsets_local"]["run_id"] == run_id_1
        assert by_stage["download_ranges"]["status"] == "failed"
        assert by_stage["download_ranges"]["run_id"] == run_id_2
        assert "timeout" in str(by_stage["download_ranges"]["error_text"] or "")
