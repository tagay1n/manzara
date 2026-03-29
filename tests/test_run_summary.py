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


def test_maintenance_sync_summary_uses_artifact_counts() -> None:
    summary = build_structured_run_summary(
        task_id="maintenance.monocorpus_sync",
        panel_id="maintenance",
        status="completed",
        exit_code=0,
        error_text=None,
        stop_mode=None,
        started_at="2026-03-29T10:00:00+00:00",
        finished_at="2026-03-29T10:00:05+00:00",
        log_lines=[],
        artifacts={
            "kind": "maintenance.sync_summary",
            "rows_added": 8,
            "rows_moved": 3,
            "rows_deleted": 1,
        },
    )
    assert summary["message"] == "Sync completed: +8 moved=3 deleted=1."
    labels = {item["label"]: item["value"] for item in summary["highlights"]}
    assert labels["Added"] == "8"
    assert labels["Moved"] == "3"
    assert labels["Deleted"] == "1"
    assert summary["artifacts"]["kind"] == "maintenance.sync_summary"
