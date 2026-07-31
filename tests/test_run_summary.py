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
