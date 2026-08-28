"""Structured run summary tests."""

from __future__ import annotations

from app.run_summary import build_structured_run_summary


def test_metadata_extraction_summary_surfaces_quota_pause_and_remaining_work() -> None:
    summary = build_structured_run_summary(
        task_id="library.metadata_extract",
        panel_id="library",
        status="completed",
        exit_code=0,
        error_text=None,
        stop_mode=None,
        started_at="2026-08-10T10:00:00+00:00",
        finished_at="2026-08-10T11:00:00+00:00",
        log_lines=[],
        artifacts={
            "kind": "library.metadata_extraction_summary",
            "outcome": "all_keys_exhausted",
            "eligible": 358,
            "processed": 53,
            "remaining": 307,
            "succeeded": 14,
            "terminal": 37,
            "corrupted_planned": 2,
            "quota_deferred": 1,
            "service_deferred": 2,
            "source_deferred": 0,
        },
    )

    assert summary["message"] == "Paused by Gemini quota with 307 documents remaining."
    assert {item["label"]: item["value"] for item in summary["highlights"]} == {
        "Eligible": "358",
        "Processed": "53",
        "Succeeded": "14",
        "Terminal": "37",
        "Corrupted planned": "2",
        "Quota deferred": "1",
        "Service deferred": "2",
        "Source deferred": "0",
        "Remaining": "307",
    }


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


def test_non_pdf_extraction_summary_uses_artifact_counts() -> None:
    summary = build_structured_run_summary(
        task_id="library.extract_non_pdf",
        panel_id="library",
        status="completed",
        exit_code=0,
        error_text=None,
        stop_mode=None,
        started_at="2026-08-25T10:00:00+00:00",
        finished_at="2026-08-25T10:10:00+00:00",
        log_lines=[],
        artifacts={
            "kind": "library.non_pdf_extraction_summary",
            "ready": 8,
            "unsupported": 3,
            "deferred": 2,
            "failed": 1,
            "corrupted": 2,
            "uploaded_images": 14,
            "deleted_stale_images": 2,
            "per_mime_limit": 100,
        },
    )

    assert summary["message"] == (
        "Non-PDF extraction completed: 8 ready, 3 unsupported, 2 deferred, "
        "1 failed."
    )
    assert {item["label"]: item["value"] for item in summary["highlights"]} == {
        "Ready": "8",
        "Unsupported": "3",
        "Deferred": "2",
        "Failed": "1",
        "Corrupted planned": "2",
        "Images uploaded": "14",
        "Stale images removed": "2",
        "Per MIME cap": "100",
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
        "checkpoint_reused": 10,
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
        "DB checkpoints": "10",
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


def test_document_upload_summary_uses_database_queue_artifact() -> None:
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
            "kind": "maintenance.document_s3_upload_summary",
            "pending_before": 12,
            "processed": 12,
            "uploaded": 7,
            "recovered_existing": 2,
            "source_cache": 6,
            "source_yandex": 5,
            "skipped_download": 2,
            "stale_upload_cleaned": 1,
            "failed": 1,
            "pending_after": 3,
            "stopped": False,
        },
    )

    assert summary["message"] == (
        "Backblaze upload completed: 9 checkpointed, 2 skipped, 1 failed, "
        "3 still pending."
    )
    assert {item["label"]: item["value"] for item in summary["highlights"]} == {
        "Pending before": "12",
        "Uploaded": "7",
        "Recovered": "2",
        "From cache": "6",
        "From Yandex": "5",
        "Skipped": "2",
        "Stale uploads cleaned": "1",
        "Failed": "1",
        "Pending after": "3",
    }


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
        panel_id="maintenance",
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
            "isbn_review_groups": 1,
            "planned_moves": {
                "total": 3,
                "by_isbn": 1,
                "by_language": 1,
                "by_non_document_format": 1,
            },
        },
    )

    assert summary["message"] == (
        "Cleanup plan prepared by ISBN, language, and document format; "
        "no storage was mutated."
    )
    highlights = {item["label"]: item["value"] for item in summary["highlights"]}
    assert highlights == {
        "Scanned": "100",
        "By ISBN": "1",
        "By language": "1",
        "Non-document": "1",
        "ISBN reviews": "1",
    }
