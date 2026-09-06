"""Shared test fixtures for Manzara."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.db import ACTIVE_STATUSES, Database
from app.modules.maintenance.config import MaintenanceSettings
from app.modules.maintenance.tasks import maintenance_task_definitions
from app.settings import Settings

TEST_POSTGRES_IMAGE = "postgres:18.6-alpine3.24"


def _test_task_defs(maintenance: MaintenanceSettings):
    return [
        *maintenance_task_definitions(maintenance),
        {
            "task_id": "maintenance.scan_test",
            "panel_id": "maintenance",
            "title": "Scan test",
            "task_type": "scan",
            "icon_idle": "RefreshCw",
            "icon_running": "Square",
            "cwd": str(maintenance.monocorpus_repo_path),
            "command": {
                "mode": "shell",
                "value": "python3 -c \"print('scan-ok')\"",
            },
        },
        {
            "task_id": "maintenance.download_test",
            "panel_id": "maintenance",
            "title": "Download test",
            "task_type": "download",
            "icon_idle": "Play",
            "icon_running": "Square",
            "cwd": str(maintenance.monocorpus_repo_path),
            "command": {
                "mode": "shell",
                "value": "python3 -c \"print('download-ok')\"",
            },
        },
        {
            "task_id": "maintenance.quick",
            "panel_id": "maintenance",
            "title": "Quick",
            "task_type": "scan",
            "icon_idle": "Play",
            "icon_running": "Square",
            "cwd": str(maintenance.monocorpus_repo_path),
            "command": {
                "mode": "shell",
                "value": "python3 -c \"print('quick-ok')\"",
            },
        },
        {
            "task_id": "maintenance.long",
            "panel_id": "maintenance",
            "title": "Long",
            "task_type": "download",
            "icon_idle": "Play",
            "icon_running": "Square",
            "cwd": str(maintenance.monocorpus_repo_path),
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
            "task_id": "maintenance.ignore_sigint",
            "panel_id": "maintenance",
            "title": "Ignore SIGINT",
            "task_type": "download",
            "icon_idle": "Play",
            "icon_running": "Square",
            "cwd": str(maintenance.monocorpus_repo_path),
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


def _start_test_postgres(
    container_factory: Callable[..., Any] | None = None,
) -> tuple[Any, str]:
    """Start the sole PostgreSQL test backend and return its generated URL."""
    container = None
    try:
        if container_factory is None:
            from testcontainers.postgres import PostgresContainer

            container_factory = PostgresContainer
        container = container_factory(
            TEST_POSTGRES_IMAGE,
            driver="psycopg2",
            username="manzara_test",
            password="manzara_test",
            dbname="manzara_test",
        )
        container.start()
        database_url = str(container.get_connection_url()).strip()
        if not database_url:
            raise RuntimeError("Testcontainers returned an empty PostgreSQL URL")
    except Exception:  # noqa: BLE001 - normalize container/runtime boundary failures.
        if container is not None:
            with suppress(Exception):
                container.stop()
        raise RuntimeError(
            "Docker and Testcontainers are required for PostgreSQL-backed tests; "
            "ensure Docker is installed, running, and accessible to this user"
        ) from None
    return container, database_url


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    """Yield the URL for one fresh PostgreSQL container per pytest session."""
    container, database_url = _start_test_postgres()
    try:
        yield database_url
    finally:
        container.stop()


def _drop_schema(database_url: str, schema_name: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    finally:
        engine.dispose()


def _truncate_schema(database_url: str, schema_name: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            table_list = conn.execute(
                text(
                    """
                    SELECT string_agg(format('%I.%I', schemaname, tablename), ', ') AS names
                    FROM pg_tables
                    WHERE schemaname = :schema
                      AND tablename <> 'alembic_version'
                    """
                ),
                {"schema": schema_name},
            ).scalar()
            if table_list:
                conn.execute(text(f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE"))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def prepared_test_schema(test_database_url: str) -> tuple[str, str]:
    """Create one migrated schema for the full test session."""
    database_url = test_database_url
    schema_name = f"manzara_test_{uuid.uuid4().hex[:10]}"
    _drop_schema(database_url, schema_name)
    db = Database(database_url, schema=schema_name)
    db.init_schema()
    try:
        yield database_url, schema_name
    finally:
        db.close()
        _drop_schema(database_url, schema_name)


@pytest.fixture()
def test_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prepared_test_schema: tuple[str, str],
) -> Iterator[tuple[TestClient, object]]:
    """Return isolated TestClient and app.main module with temporary state."""
    from app import main as main_app

    database_url, schema_name = prepared_test_schema
    _truncate_schema(database_url, schema_name)

    monocorpus_repo = tmp_path / "monocorpus"
    monocorpus_repo.mkdir(parents=True, exist_ok=True)
    maintenance = MaintenanceSettings(
        monocorpus_repo_path=monocorpus_repo,
        pgbackrest_stanza="monocorpus",
    )
    settings = Settings(
        database_url=database_url,
        database_schema=schema_name,
        maintenance=maintenance,
    )

    monkeypatch.setattr(main_app, "maintenance_task_definitions", _test_task_defs)
    main_app.state = main_app.AppState(settings)
    main_app.state.runner._artifacts_root = tmp_path / "_artifacts" / "task_runs"
    main_app.state.runner._artifacts_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main_app.state.db, "init_schema", lambda: None)

    with TestClient(main_app.app) as client:
        try:
            yield client, main_app
        finally:
            # Stop tasks while the application pool is still accepting checkouts.
            for _ in range(20):
                active = main_app.state.db.list_active_runs()
                if not active:
                    break
                main_app.state.runner.stop_all_toggle()
                time.sleep(0.15)
    _truncate_schema(database_url, schema_name)


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
        logs = main_app.state.runner.get_run_logs(
            task_id=str((run or {}).get("task_id") or "unknown"),
            run_id=run_id,
            limit=20,
        )
        raise AssertionError(
            f"Run {run_id} did not reach terminal state; "
            f"last_run={run}; logs={logs}"
        )

    return _wait
