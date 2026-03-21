"""Shared test fixtures for Manzara."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator, Tuple

import pytest
from fastapi.testclient import TestClient

from app.modules.maintenance.config import MaintenanceSettings
from app.modules.shayan.config import ShayanSettings
from app.settings import Settings


def _test_task_defs(shayan: ShayanSettings):
    return [
        {
            "task_id": "shayan.scan_changes",
            "panel_id": "shayan",
            "title": "Scan for changes",
            "task_type": "scan",
            "icon_idle": "RefreshCw",
            "icon_running": "Square",
            "cwd": str(shayan.repo_path),
            "command": {
                "mode": "shell",
                "value": (
                    "python3 -c \"import pathlib; "
                    "p=pathlib.Path('_artifacts/snapshots/latest.json'); "
                    "p.parent.mkdir(parents=True, exist_ok=True); "
                    "p.write_text('{\\\"entries\\\": {}}', encoding='utf-8'); "
                    "print('scan-ok')\""
                ),
            },
        },
        {
            "task_id": "shayan.download_new",
            "panel_id": "shayan",
            "title": "Download new",
            "task_type": "download",
            "icon_idle": "Play",
            "icon_running": "Square",
            "cwd": str(shayan.repo_path),
            "command": {
                "mode": "shell",
                "value": "python3 -c \"print('download-ok')\"",
            },
        },
        {
            "task_id": "shayan.quick",
            "panel_id": "shayan",
            "title": "Quick",
            "task_type": "scan",
            "icon_idle": "Play",
            "icon_running": "Square",
            "cwd": str(shayan.repo_path),
            "command": {
                "mode": "shell",
                "value": "python3 -c \"print('quick-ok')\"",
            },
        },
        {
            "task_id": "shayan.long",
            "panel_id": "shayan",
            "title": "Long",
            "task_type": "download",
            "icon_idle": "Play",
            "icon_running": "Square",
            "cwd": str(shayan.repo_path),
            "command": {
                "mode": "shell",
                "value": (
                    "python3 -c \"import time,sys; "
                    "print('long-start'); "
                    "sys.stdout.flush(); "
                    "time.sleep(30)\""
                ),
            },
        },
        {
            "task_id": "shayan.ignore_sigint",
            "panel_id": "shayan",
            "title": "Ignore SIGINT",
            "task_type": "download",
            "icon_idle": "Play",
            "icon_running": "Square",
            "cwd": str(shayan.repo_path),
            "command": {
                "mode": "shell",
                "value": (
                    "python3 -c \"import signal,time,sys; "
                    "signal.signal(signal.SIGINT, lambda _sig, _frame: print('sigint-ignored', flush=True)); "
                    "print('ignore-start', flush=True); "
                    "time.sleep(30)\""
                ),
            },
        },
    ]


@pytest.fixture()
def test_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Tuple[TestClient, object]]:
    """Return isolated TestClient and app.main module with temporary state."""
    from app import main as main_app

    shayan_repo = tmp_path / "shayan"
    artifacts = shayan_repo / "_artifacts"
    snapshots = artifacts / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    (artifacts / "status.json").write_text("{}", encoding="utf-8")
    (artifacts / "last-main-run-summary.json").write_text("{}", encoding="utf-8")

    shayan = ShayanSettings(
        repo_path=shayan_repo,
        output_path=tmp_path / "output",
        status_file=artifacts / "status.json",
        summary_file=artifacts / "last-main-run-summary.json",
        latest_snapshot_file=artifacts / "snapshots" / "latest.json",
    )
    maintenance = MaintenanceSettings(monocorpus_repo_path=tmp_path / "monocorpus")
    settings = Settings(
        db_path=tmp_path / "manzara-test.db",
        shayan=shayan,
        maintenance=maintenance,
        scheduler_enabled=False,
    )

    monkeypatch.setattr(main_app, "shayan_task_definitions", _test_task_defs)
    main_app.state = main_app.AppState(settings)

    with TestClient(main_app.app) as client:
        yield client, main_app

    # Best-effort teardown: request graceful then force until no active runs.
    for _ in range(20):
        active = main_app.state.db.list_active_runs()
        if not active:
            break
        main_app.state.runner.stop_all_toggle()
        time.sleep(0.15)


@pytest.fixture()
def wait_for_terminal_run() -> callable:
    """Wait helper for run completion in tests."""

    def _wait(main_app, run_id: int, timeout_seconds: float = 15.0):
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            run = main_app.state.db.get_run(run_id)
            if run and run["status"] not in {
                "starting",
                "running",
                "stopping_graceful",
                "stopping_force",
            }:
                return run
            time.sleep(0.05)
        run = main_app.state.db.get_run(run_id)
        logs = main_app.state.db.get_logs(run_id, limit=20)
        raise AssertionError(
            f"Run {run_id} did not reach terminal state; "
            f"last_run={run}; logs={logs}"
        )

    return _wait


@pytest.fixture()
def wait_for_terminal_workflow_run() -> callable:
    """Wait helper for workflow completion in tests."""

    def _wait(main_app, workflow_run_id: int, timeout_seconds: float = 15.0):
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            run = main_app.state.db.get_workflow_run(workflow_run_id)
            if run and run["status"] not in {"starting", "running"}:
                return run
            time.sleep(0.05)
        run = main_app.state.db.get_workflow_run(workflow_run_id)
        raise AssertionError(
            f"Workflow run {workflow_run_id} did not reach terminal state; "
            f"last_run={run}"
        )

    return _wait
