from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.db import Database
from app.local_state import (
    LOCAL_STATE_SCHEMA_VERSION,
    AIItemCheckpointStore,
    LocalStateStore,
)


def test_local_state_is_private_wal_database_with_runtime_tables(tmp_path: Path) -> None:
    path = tmp_path / "manzara" / "state" / "runtime.sqlite3"
    store = LocalStateStore(path)
    store.initialize()

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LOCAL_STATE_SCHEMA_VERSION
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"runs", "events", "gemini_keys", "ai_item_checkpoints"} <= tables


def test_ai_attempts_are_local_versioned_and_disposable(tmp_path: Path) -> None:
    path = tmp_path / "state" / "runtime.sqlite3"
    LocalStateStore(path).initialize()
    checkpoints = AIItemCheckpointStore(path)

    checkpoints.record_failure(
        flow_id="library.metadata_extract",
        item_id="abc",
        contract_version="prompt.v1",
        model_name="model-a",
        kind="invalid",
        error="bad response",
        models=["model-a", "model-b"],
        run_id=12,
    )
    checkpoints.record_failure(
        flow_id="library.metadata_extract",
        item_id="abc",
        contract_version="prompt.v1",
        model_name="model-a",
        kind="invalid",
        error="duplicate",
        models=["model-a", "model-b"],
        run_id=12,
    )
    state = checkpoints.get("library.metadata_extract", "abc")
    assert [attempt["model"] for attempt in state["attempts"]] == ["model-a"]

    checkpoints.clear("library.metadata_extract", "abc")
    assert checkpoints.get("library.metadata_extract", "abc") is None


def test_concurrent_ai_tasks_serialize_checkpoint_updates(tmp_path: Path) -> None:
    path = tmp_path / "state" / "runtime.sqlite3"
    LocalStateStore(path).initialize()
    checkpoints = AIItemCheckpointStore(path)

    def record(model_number: int) -> None:
        checkpoints.record_failure(
            flow_id="library.metadata_extract",
            item_id="shared-document",
            contract_version="prompt.v1",
            model_name=f"model-{model_number}",
            kind="invalid",
            error="bad response",
            models=[f"model-{number}" for number in range(8)],
            run_id=model_number,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record, range(8)))

    state = checkpoints.get("library.metadata_extract", "shared-document")
    assert {attempt["model"] for attempt in state["attempts"]} == {
        f"model-{number}" for number in range(8)
    }


def test_operational_repositories_do_not_open_postgres(tmp_path: Path) -> None:
    path = tmp_path / "state" / "runtime.sqlite3"
    LocalStateStore(path).initialize()

    def reject_cloud_connection(_database_url: str):
        raise AssertionError("operational state must not connect to PostgreSQL")

    db = Database(
        "postgresql://unused/runtime",
        connection_factory=reject_cloud_connection,
        local_state_path=path,
    )
    try:
        db.seed_panels([{"panel_id": "library", "title": "Library"}])
        db.seed_tasks(
            [
                {
                    "task_id": "library.local",
                    "panel_id": "library",
                    "title": "Local task",
                    "task_type": "test",
                    "icon_idle": "Play",
                    "icon_running": "Square",
                    "command": {"mode": "shell", "value": "true"},
                    "cwd": str(tmp_path),
                }
            ]
        )
        run_id = db.create_run(db.get_task("library.local"))
        db.insert_event(
            "task.started",
            task_id="library.local",
            run_id=run_id,
            panel_id="library",
            payload={},
        )
        db.upsert_gemini_keys(
            [{"key_id": "account:key", "account_id": "account", "masked_key": "***"}]
        )
        db.ensure_gemini_model_states(["account:key"], "model")

        assert db.get_run(run_id)["task_id"] == "library.local"
        assert db.get_latest_event_id() > 0
        assert db.list_gemini_model_states(model_name="model")[0]["key_id"] == "account:key"
    finally:
        db.close()
