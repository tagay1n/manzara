"""Database-layer tests for run recovery behavior."""

from __future__ import annotations

from pathlib import Path

from app.db import Database


def test_recover_active_runs_marks_running_as_failed(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init_schema()

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
