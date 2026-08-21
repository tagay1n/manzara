"""Tests for Shayan task definitions."""

from __future__ import annotations

from pathlib import Path

from app.modules.shayan.config import ShayanSettings
from app.modules.shayan.tasks import shayan_task_definitions
from app.modules.maintenance.config import MaintenanceSettings
from app.modules.maintenance.tasks import maintenance_task_definitions
from app.modules.library.tasks import library_task_definitions


def test_shayan_tasks_include_direct_hetzner_upload_without_obsolete_migration() -> (
    None
):
    settings = ShayanSettings(
        repo_path=Path("/tmp/shayan-repo"),
        output_path=Path("/tmp/shayan-output"),
        artifacts_dir=Path("/tmp/.manzara/shayan"),
    )
    tasks = shayan_task_definitions(settings)
    by_id = {item["task_id"]: item for item in tasks}
    assert "shayan.upload_yadisk" in by_id
    upload = by_id["shayan.upload_yadisk"]
    command = str(upload["command"]["value"])
    assert upload["title"] == "Upload"
    assert "app.modules.shayan.runtime.upload_webdav" in command
    assert "--output-path /tmp/shayan-output" in command
    assert "upload_yadisk" not in command
    assert "shayan.transfer_yadisk_webdav" not in by_id


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


def test_maintenance_tasks_include_dump_state() -> None:
    tasks = maintenance_task_definitions(
        MaintenanceSettings(
            monocorpus_repo_path=Path("/tmp/monocorpus"),
            pgbackrest_stanza="monocorpus",
        )
    )

    task = {item["task_id"]: item for item in tasks}["maintenance.dump_state"]
    assert task["panel_id"] == "backup"
    assert task["title"] == "Upload to GSheets"
    assert "app.modules.maintenance.runtime.dump_state" in str(
        task["command"]["value"]
    )
    assert "--legacy-credentials-dir /tmp/monocorpus/_artifacts/credentials" in str(
        task["command"]["value"]
    )


def test_backup_tasks_have_a_dedicated_catalog_and_short_titles() -> None:
    tasks = maintenance_task_definitions(
        MaintenanceSettings(
            monocorpus_repo_path=Path("/tmp/monocorpus"),
            pgbackrest_stanza="monocorpus",
        )
    )
    by_id = {item["task_id"]: item for item in tasks}

    full = by_id["maintenance.pgbackrest_backup_full"]
    assert full["panel_id"] == "backup"
    assert full["title"] == "Full backup"

    incremental = by_id["maintenance.pgbackrest_backup_incr"]
    assert incremental["panel_id"] == "backup"
    assert incremental["title"] == "Incremental backup"


def test_library_tasks_include_metadata_extraction() -> None:
    task = {
        item["task_id"]: item
        for item in library_task_definitions(app_root=Path("/tmp/manzara"))
    }["library.metadata_extract"]

    assert task["panel_id"] == "library"
    assert task["title"] == "Extract metadata"
    assert "app.modules.library.runtime.run_metadata_extract" in str(
        task["command"]["value"]
    )
