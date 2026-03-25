"""Shared test fixtures for Manzara."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Iterator, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.db import ACTIVE_STATUSES, ACTIVE_WORKFLOW_STATUSES
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
                    f"p=pathlib.Path({shayan.latest_snapshot_file.as_posix()!r}); "
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
                    "time.sleep(8)\""
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
                    "time.sleep(8)\""
                ),
            },
        },
    ]


def _contains_redacted(node: object) -> bool:
    if isinstance(node, str):
        return "<REDACTED>" in node
    if isinstance(node, dict):
        return any(_contains_redacted(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_redacted(value) for value in node)
    return False


def _resolve_test_database_url() -> str:
    def _with_connect_timeout(database_url: str, seconds: int = 3) -> str:
        split = urlsplit(database_url)
        params = dict(parse_qsl(split.query, keep_blank_values=True))
        if "connect_timeout" not in params:
            params["connect_timeout"] = str(int(seconds))
        return urlunsplit(
            (
                split.scheme,
                split.netloc,
                split.path,
                urlencode(params),
                split.fragment,
            )
        )

    for env_name in ("MANZARA_TEST_DATABASE_URL", "MANZARA_DATABASE_URL"):
        value = str(os.environ.get(env_name) or "").strip()
        if value:
            return _with_connect_timeout(value)

    config_override = os.environ.get("MANZARA_CONFIG_PATH")
    candidates: list[Path]
    if config_override:
        candidates = [Path(config_override).expanduser()]
    else:
        candidates = [
            Path("config.local.yaml"),
            Path("config.yaml"),
            Path("config.example.yaml"),
        ]

    for candidate in candidates:
        if not candidate.exists():
            continue
        data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            continue
        if _contains_redacted(data):
            continue
        database_url = str(data.get("database_url") or "").strip()
        if database_url:
            return _with_connect_timeout(database_url)
    raise RuntimeError(
        "Tests require database_url. Set MANZARA_TEST_DATABASE_URL or MANZARA_DATABASE_URL."
    )


def _drop_schema(database_url: str, schema_name: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    finally:
        engine.dispose()


@pytest.fixture()
def test_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Tuple[TestClient, object]]:
    """Return isolated TestClient and app.main module with temporary state."""
    from app import main as main_app

    database_url = _resolve_test_database_url()
    schema_name = f"manzara_test_{uuid.uuid4().hex[:10]}"

    shayan_repo = tmp_path / "shayan"
    artifacts = tmp_path / ".manzara" / "shayan"
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
    maintenance = MaintenanceSettings(
        monocorpus_repo_path=tmp_path / "monocorpus",
        pgbackrest_stanza="monocorpus",
    )
    settings = Settings(
        database_url=database_url,
        database_schema=schema_name,
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

    _drop_schema(database_url, schema_name)


@pytest.fixture()
def wait_for_terminal_run() -> callable:
    """Wait helper for run completion in tests."""

    def _wait(main_app, run_id: int, timeout_seconds: float = 15.0):
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            run = main_app.state.db.get_run(run_id)
            if run and run["status"] not in ACTIVE_STATUSES:
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
            if run and run["status"] not in ACTIVE_WORKFLOW_STATUSES:
                return run
            time.sleep(0.05)
        run = main_app.state.db.get_workflow_run(workflow_run_id)
        raise AssertionError(
            f"Workflow run {workflow_run_id} did not reach terminal state; "
            f"last_run={run}"
        )

    return _wait
