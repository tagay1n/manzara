"""Tests for Shayan task definitions."""

from __future__ import annotations

from pathlib import Path

from app.modules.shayan.config import ShayanSettings
from app.modules.shayan.tasks import shayan_task_definitions


def test_shayan_tasks_include_upload_yadisk_stage() -> None:
    settings = ShayanSettings(
        repo_path=Path("/tmp/shayan-repo"),
        output_path=Path("/tmp/shayan-output"),
        artifacts_dir=Path("/tmp/.manzara/shayan"),
    )
    tasks = shayan_task_definitions(settings)
    by_id = {item["task_id"]: item for item in tasks}
    assert "shayan.upload_yadisk" in by_id
    command = str(by_id["shayan.upload_yadisk"]["command"]["value"])
    assert "--stage upload_yadisk" in command
