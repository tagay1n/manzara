from __future__ import annotations

import time
from pathlib import Path

from app.attention import AttentionProvider, AttentionService
from app.db import Database
from app.local_state import LocalStateStore


def _db(tmp_path: Path) -> Database:
    path = tmp_path / "runtime.sqlite3"
    LocalStateStore(path).initialize()
    return Database(
        "postgresql://unused/runtime",
        connection_factory=lambda _url: (_ for _ in ()).throw(
            AssertionError("test provider should not connect to PostgreSQL")
        ),
        local_state_path=path,
    )


def test_attention_service_coalesces_events_and_publishes_changed_snapshot(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    calls: list[int] = []

    def load(source_event_id: int):
        calls.append(source_event_id)
        return [
            {
                "signal_id": "library.previews",
                "task_id": "library.generate_book_previews",
                "panel_id": "library",
                "section_id": "overview",
                "kind": "count",
                "count": source_event_id,
                "label": "Book previews available",
                "href": "/tasks/generate-book-previews",
            }
        ]

    service = AttentionService(
        db,
        [AttentionProvider("library", load, frozenset({"task.completed"}))],
        initial_delay_seconds=0,
        poll_interval_seconds=0.01,
    )
    try:
        service.start()
        db.insert_event("task.completed", "one", 1, "library", {})
        db.insert_event("task.completed", "two", 2, "library", {})
        deadline = time.time() + 2
        while time.time() < deadline:
            events = db.get_events_after(0, limit=100)
            if db.get_attention_snapshot()["signals"] and any(
                item["type"] == "attention.updated" for item in events
            ):
                break
            time.sleep(0.01)
        snapshot = db.get_attention_snapshot()
        assert snapshot["signals"][0]["count"] >= 2
        assert len(calls) <= 2
        assert any(item["type"] == "attention.updated" for item in events)
    finally:
        service.shutdown()
        db.close()


def test_failed_task_dot_clears_after_success(tmp_path: Path) -> None:
    db = _db(tmp_path)
    service = AttentionService(
        db, [], initial_delay_seconds=0, poll_interval_seconds=0.01
    )
    try:
        service.start()
        failed = db.insert_event(
            "task.failed", "library.site_export", 10, "library", {}
        )
        service.process_events_once()
        signals = db.get_attention_snapshot()["signals"]
        assert signals[0]["kind"] == "dot"
        assert signals[0]["source_event_id"] == failed["event_id"]

        db.insert_event(
            "task.completed", "library.site_export", 11, "library", {}
        )
        service.process_events_once()
        assert db.get_attention_snapshot()["signals"] == []
    finally:
        service.shutdown()
        db.close()


def test_provider_failure_preserves_last_value_as_stale(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.replace_attention_provider_signals(
        "library",
        [
            {
                "signal_id": "library.previews",
                "task_id": "library.generate_book_previews",
                "panel_id": "library",
                "section_id": "overview",
                "kind": "count",
                "count": 8,
                "label": "Book previews available",
                "href": "/tasks/library.generate_book_previews",
            }
        ],
        source_event_id=1,
    )

    def fail(_source_event_id: int):
        raise RuntimeError("database unavailable")

    service = AttentionService(
        db,
        [AttentionProvider("library", fail, frozenset())],
        initial_delay_seconds=0,
    )
    try:
        assert service._refresh_one() is True
        snapshot = db.get_attention_snapshot()
        assert snapshot["signals"][0]["count"] == 8
        assert snapshot["signals"][0]["status"] == "stale"
        assert snapshot["providers"]["library"]["error_text"].endswith(
            "database unavailable"
        )
    finally:
        db.close()
