"""Tests for Shayan task definitions."""

from __future__ import annotations

from pathlib import Path

from app.modules.shayan.config import ShayanSettings
from app.modules.shayan.tasks import shayan_task_definitions
from app.modules.maintenance.config import MaintenanceSettings
from app.modules.maintenance.tasks import maintenance_task_definitions


def test_shayan_tasks_include_storage_transfer_stage() -> None:
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

    transfer = by_id["shayan.transfer_yadisk_webdav"]
    assert transfer["task_type"] == "transfer"
    assert transfer["title"] == "Copy Yandex Disk videos to Nextcloud"
    assert transfer["icon_idle"] == "CloudCog"
    assert "app.modules.shayan.runtime.transfer_yadisk_webdav" in str(
        transfer["command"]["value"]
    )


def test_maintenance_tasks_include_document_s3_sync() -> None:
    tasks = maintenance_task_definitions(
        MaintenanceSettings(
            monocorpus_repo_path=Path("/tmp/monocorpus"),
            pgbackrest_stanza="monocorpus",
        )
    )
    task = {item["task_id"]: item for item in tasks}["maintenance.sync_documents_s3"]
    assert task["panel_id"] == "maintenance"
    assert task["task_type"] == "transfer"
    assert "app.modules.maintenance.runtime.sync_documents_s3" in str(
        task["command"]["value"]
    )
