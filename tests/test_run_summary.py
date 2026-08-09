"""Structured run summary tests."""

from __future__ import annotations

from app.run_summary import build_structured_run_summary


def test_shayan_scan_summary_uses_artifact_counts() -> None:
    summary = build_structured_run_summary(
        task_id="shayan.scan_changes",
        panel_id="shayan",
        status="completed",
        exit_code=0,
        error_text=None,
        stop_mode=None,
        started_at="2026-03-26T08:00:00+00:00",
        finished_at="2026-03-26T08:00:05+00:00",
        log_lines=[],
        artifacts={
            "kind": "shayan.snapshot_diff",
            "episodes_added": 4,
            "episodes_changed": 1,
            "episodes_removed": 2,
        },
    )
    assert summary["message"] == "Scan completed: +4 ~1 -2."
    labels = {item["label"]: item["value"] for item in summary["highlights"]}
    assert labels["Added"] == "4"
    assert labels["Changed"] == "1"
    assert labels["Removed"] == "2"
    assert summary["artifacts"]["kind"] == "shayan.snapshot_diff"


def test_shayan_upload_summary_uses_artifact_counts() -> None:
    summary = build_structured_run_summary(
        task_id="shayan.upload_yadisk",
        panel_id="shayan",
        status="completed",
        exit_code=0,
        error_text=None,
        stop_mode=None,
        started_at="2026-03-26T08:00:00+00:00",
        finished_at="2026-03-26T08:00:05+00:00",
        log_lines=[],
        artifacts={
            "kind": "shayan.upload_yadisk_summary",
            "uploaded": 7,
            "failed": 2,
            "missing_local": 1,
        },
    )
    assert summary["message"] == "Upload completed: 7 uploaded, 2 failed."
    labels = {item["label"]: item["value"] for item in summary["highlights"]}
    assert labels["Uploaded"] == "7"
    assert labels["Failed"] == "2"
    assert labels["Missing local"] == "1"
    assert summary["artifacts"]["kind"] == "shayan.upload_yadisk_summary"


def test_library_preview_summary_uses_artifact_counts() -> None:
    summary = build_structured_run_summary(
        task_id="library.generate_book_previews",
        panel_id="library",
        status="completed",
        exit_code=0,
        error_text=None,
        stop_mode=None,
        started_at="2026-07-31T12:00:00+00:00",
        finished_at="2026-07-31T12:01:00+00:00",
        log_lines=[],
        artifacts={
            "kind": "library.book_preview_summary",
            "ready": 7,
            "partial": 1,
            "failed": 2,
            "uploaded_objects": 24,
            "reused_objects": 6,
        },
    )

    assert summary["message"] == "Book previews completed: 7 ready, 1 partial, 2 failed."
    assert {item["label"]: item["value"] for item in summary["highlights"]} == {
        "Ready": "7",
        "Partial": "1",
        "Failed": "2",
        "Uploaded": "24",
        "Reused": "6",
    }


def test_shayan_webdav_summary_uses_artifact_counts() -> None:
    summary = build_structured_run_summary(
        task_id="shayan.transfer_yadisk_webdav",
        panel_id="shayan",
        status="completed",
        exit_code=0,
        error_text=None,
        stop_mode=None,
        started_at=None,
        finished_at=None,
        log_lines=[],
        artifacts={
            "kind": "shayan.yadisk_webdav_transfer_summary",
            "copied": 7,
            "reused": 3,
            "failed": 1,
        },
    )

    assert summary["message"] == (
        "Nextcloud copy completed: 7 copied, 3 reused, 1 failed."
    )
    assert {item["label"]: item["value"] for item in summary["highlights"]} == {
        "Copied": "7",
        "Reused": "3",
        "Failed": "1",
    }


def test_document_sync_summary_uses_structured_artifact() -> None:
    artifacts = {
        "kind": "maintenance.document_s3_sync_summary",
        "source_files": 17,
        "source_documents": 15,
        "database_rows_after": 16,
        "synced_source_documents": 13,
        "unsynced_source_documents": 2,
        "database_only_rows": 3,
        "fully_synced": False,
        "duplicates": 2,
        "verified": 12,
        "uploaded": 3,
        "reuploaded": 1,
        "updated": 9,
        "failed": 2,
        "private_cleaned": 4,
    }
    summary = build_structured_run_summary(
        task_id="maintenance.sync_documents_s3",
        panel_id="maintenance",
        status="completed",
        exit_code=0,
        error_text=None,
        stop_mode=None,
        started_at=None,
        finished_at=None,
        log_lines=[],
        artifacts=artifacts,
    )
    assert summary["message"] == (
        "Document reconciliation completed with differences: "
        "13/15 source documents synced, 3 database-only, 2 failed."
    )
    assert {item["label"]: item["value"] for item in summary["highlights"]} == {
        "Source files": "17",
        "Source documents": "15",
        "Database rows": "16",
        "Synced": "13",
        "Unsynced": "2",
        "Database-only": "3",
        "Duplicate paths": "2",
        "Uploaded": "3",
        "DB updated": "9",
        "Failed": "2",
        "Result": "Differences",
    }

    failed = build_structured_run_summary(
        task_id="maintenance.sync_documents_s3",
        panel_id="maintenance",
        status="failed",
        exit_code=1,
        error_text="2 files failed",
        stop_mode=None,
        started_at=None,
        finished_at=None,
        log_lines=[],
        artifacts=artifacts,
    )
    assert failed["message"] == "Document sync failed before reconciliation completed."
    assert failed["highlights"]


def test_document_sync_summary_marks_incomplete_discovery_as_not_evaluated() -> None:
    summary = build_structured_run_summary(
        task_id="maintenance.sync_documents_s3",
        panel_id="maintenance",
        status="stopped",
        exit_code=0,
        error_text=None,
        stop_mode="graceful",
        started_at=None,
        finished_at=None,
        log_lines=[],
        artifacts={
            "kind": "maintenance.document_s3_sync_summary",
            "discovery_complete": False,
            "source_files": 10,
            "source_documents": 9,
            "database_rows_after": 100,
            "synced_source_documents": 8,
            "unsynced_source_documents": 1,
            "database_only_rows": None,
            "duplicates": 1,
            "fully_synced": False,
        },
    )

    assert summary["message"] == (
        "Document sync stopped after 10 discovered files; "
        "full reconciliation was not evaluated."
    )
    highlights = {item["label"]: item["value"] for item in summary["highlights"]}
    assert highlights["Database-only"] == "Not evaluated"
    assert highlights["Result"] == "Discovery incomplete"


def test_document_sync_summary_reports_unavailable_yandex_paths() -> None:
    summary = build_structured_run_summary(
        task_id="maintenance.sync_documents_s3",
        panel_id="maintenance",
        status="completed",
        exit_code=0,
        error_text=None,
        stop_mode=None,
        started_at=None,
        finished_at=None,
        log_lines=[],
        artifacts={
            "kind": "maintenance.document_s3_sync_summary",
            "discovery_complete": False,
            "source_files": 10,
            "source_documents": 9,
            "database_rows_after": 100,
            "synced_source_documents": 9,
            "unsynced_source_documents": 0,
            "database_only_rows": None,
            "duplicates": 0,
            "discovery_failed": 2,
            "failed": 2,
            "fully_synced": False,
        },
    )

    assert summary["message"] == (
        "Document discovery completed with 2 unavailable Yandex paths; "
        "full reconciliation was not evaluated."
    )
    highlights = {item["label"]: item["value"] for item in summary["highlights"]}
    assert highlights["Discovery failures"] == "2"
    assert highlights["Database-only"] == "Not evaluated"
def test_document_cleanup_preparation_summary_uses_structured_artifact() -> None:
    summary = build_structured_run_summary(
        task_id="library.prepare_document_cleanup",
        panel_id="library",
        status="completed",
        exit_code=0,
        error_text=None,
        stop_mode=None,
        started_at="2026-08-08T10:00:00+00:00",
        finished_at="2026-08-08T10:00:01+00:00",
        log_lines=[],
        artifacts={
            "kind": "library.document_cleanup_preparation_summary",
            "scanned": 100,
            "plans_created": 3,
            "isbn_groups": 2,
            "isbn_reviews_created": 1,
        },
    )

    assert summary["message"] == "Document cleanup plans prepared; no storage was mutated."
    assert {item["label"]: item["value"] for item in summary["highlights"]}["Plans"] == "3"
