"""Run artifact extraction tests."""

from __future__ import annotations

import json
from pathlib import Path

from app.run_artifacts import capture_pre_run_artifacts, collect_post_run_artifacts


def _write_snapshot(path: Path, entries: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at": "2026-03-26T00:00:00+00:00",
        "entries": entries,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_shayan_scan_artifacts_include_added_changed_removed(tmp_path: Path) -> None:
    snapshot = tmp_path / "latest.json"
    _write_snapshot(
        snapshot,
        {
            "ep-1": {"title": "Episode 1", "hls": "https://example.org/1.m3u8"},
            "ep-2": {"title": "Episode 2", "hls": "https://example.org/2.m3u8"},
        },
    )

    task = {
        "task_id": "shayan.scan_changes",
        "panel_id": "shayan",
        "command": {
            "mode": "shell",
            "value": "echo scan",
            "artifacts": {"snapshot_file": str(snapshot)},
        },
    }
    pre_state = capture_pre_run_artifacts(task)

    _write_snapshot(
        snapshot,
        {
            "ep-2": {"title": "Episode 2", "hls": "https://example.org/2-v2.m3u8"},
            "ep-3": {"title": "Episode 3", "hls": "https://example.org/3.m3u8"},
        },
    )

    artifacts = collect_post_run_artifacts(
        task,
        status="completed",
        pre_state=pre_state,
    )
    assert artifacts["kind"] == "shayan.snapshot_diff"
    assert artifacts["episodes_before"] == 2
    assert artifacts["episodes_after"] == 2
    assert artifacts["episodes_added"] == 1
    assert artifacts["episodes_changed"] == 1
    assert artifacts["episodes_removed"] == 1
    assert "ep-3" in artifacts["added_sample_ids"]
    assert "ep-2" in artifacts["changed_sample_ids"]
    assert "ep-1" in artifacts["removed_sample_ids"]


def test_shayan_download_artifacts_reads_summary_file(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps({"episodes": {"downloaded": 12, "failed": 3}}, ensure_ascii=False),
        encoding="utf-8",
    )
    task = {
        "task_id": "shayan.download_new",
        "panel_id": "shayan",
        "command": {
            "mode": "shell",
            "value": "echo download",
            "artifacts": {"summary_file": str(summary)},
        },
    }

    artifacts = collect_post_run_artifacts(
        task,
        status="completed",
        pre_state={},
    )
    assert artifacts["kind"] == "shayan.download_summary"
    assert artifacts["downloaded"] == 12
    assert artifacts["failed"] == 3


def test_embedded_log_artifacts_take_precedence(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    _write_snapshot(snapshot, {"ep-1": {"title": "Episode 1"}})
    task = {
        "task_id": "shayan.scan_changes",
        "panel_id": "shayan",
        "command": {
            "mode": "shell",
            "value": "echo scan",
            "artifacts": {"snapshot_file": str(snapshot)},
        },
    }
    pre_state = capture_pre_run_artifacts(task)
    _write_snapshot(snapshot, {"ep-2": {"title": "Episode 2"}})
    artifacts = collect_post_run_artifacts(
        task,
        status="completed",
        pre_state=pre_state,
        log_lines=[
            "line 1",
            'MANZARA_RUN_ARTIFACTS_JSON={"kind":"custom","value":42}',
        ],
    )
    assert artifacts == {"kind": "custom", "value": 42}


def test_maintenance_sync_artifacts_extract_from_logs() -> None:
    task = {
        "task_id": "maintenance.monocorpus_sync",
        "panel_id": "maintenance",
        "command": {
            "mode": "shell",
            "value": "echo sync",
        },
    }
    artifacts = collect_post_run_artifacts(
        task,
        status="completed",
        pre_state={},
        log_lines=[
            "some line",
            (
                "MANZARA_RUN_ARTIFACTS_JSON="
                '{"kind":"maintenance.sync_summary","rows_added":11,"rows_moved":4,"rows_deleted":2}'
            ),
        ],
    )
    assert artifacts["kind"] == "maintenance.sync_summary"
    assert artifacts["rows_added"] == 11
    assert artifacts["rows_moved"] == 4
    assert artifacts["rows_deleted"] == 2
