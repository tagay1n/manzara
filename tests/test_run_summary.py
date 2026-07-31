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


def test_document_sync_summary_uses_structured_artifact() -> None:
    artifacts = {
        "kind": "maintenance.document_s3_sync_summary",
        "verified": 12,
        "uploaded": 3,
        "reuploaded": 1,
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
    assert summary["message"] == "Document sync completed: 12 verified, 3 uploaded, 2 failed."
    assert {item["label"]: item["value"] for item in summary["highlights"]} == {
        "Verified": "12",
        "Uploaded": "3",
        "Re-uploaded": "1",
        "Private cleaned": "4",
        "Failed": "2",
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
    assert failed["message"] == "Document sync failed: 12 verified, 3 uploaded, 2 failed."
    assert failed["highlights"]
