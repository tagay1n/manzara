from __future__ import annotations

import pytest

from app.gemini_runtime import (
    GeminiLease,
    GeminiRuntimeManager,
    GeminiStopRequestedError,
    GeminiTransportError,
)


def test_gemini_wait_is_interruptible_for_graceful_stop() -> None:
    manager = GeminiRuntimeManager(
        object(),
        task_id="library.collection_validate",
        panel_id="library",
        should_stop=lambda: True,
    )

    with pytest.raises(GeminiStopRequestedError):
        manager._sleep_until(None)


def test_connection_reset_is_classified_as_transient_transport_failure() -> None:
    class Db:
        def __init__(self) -> None:
            self.errors = []
            self.events = []

        def mark_gemini_error(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.errors.append((args, kwargs))

        def insert_event(self, event_type, **kwargs):  # noqa: ANN001
            self.events.append((event_type, kwargs))

    db = Db()
    manager = GeminiRuntimeManager(db, task_id="library.metadata_extract", panel_id="library")
    lease = GeminiLease("account", "key-id", "secret", "secr...cret", "model")

    with pytest.raises(GeminiTransportError, match="Connection reset by peer"):
        manager._handle_error(
            lease=lease,
            error=ConnectionResetError(104, "Connection reset by peer"),
            run_id=55,
        )

    assert db.errors
    assert db.events[-1][0] == "gemini.request.transport_error"
