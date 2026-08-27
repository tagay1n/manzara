from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.gemini_runtime import (
    GeminiLease,
    GeminiRuntimeManager,
    GeminiStopRequestedError,
    GeminiTransportError,
)
from app.gemini_config import GeminiKey


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


def test_manual_blackout_override_disables_current_window(monkeypatch) -> None:
    now = datetime(2026, 8, 12, 7, 30, tzinfo=timezone.utc)

    class Db:
        def __init__(self) -> None:
            self.control = {
                "cycle_label": "2026-08-12",
                "pause_until": None,
                "last_pause_reason": None,
                "blackout_override_until": None,
            }
            self.events = []

        def ensure_gemini_runtime_control(self, _cycle_label):  # noqa: ANN001
            return dict(self.control)

        def rollover_gemini_cycle(self, _cycle_label):  # noqa: ANN001
            return False

        def set_gemini_blackout_override(self, override_until):  # noqa: ANN001
            self.control["blackout_override_until"] = override_until
            return dict(self.control)

        def insert_event(self, event_type, **kwargs):  # noqa: ANN001
            self.events.append((event_type, kwargs))

    db = Db()
    manager = GeminiRuntimeManager(db, task_id=None, panel_id="library")
    monkeypatch.setattr("app.gemini_runtime._utc_now", lambda: now)

    result = manager.override_blackout()

    assert result["blackout_override_until"] == "2026-08-12T08:00:00+00:00"
    assert db.events[-1][0] == "gemini.blackout.overridden"
    assert manager._wait_reason(db.control, now) is None


def test_snapshot_calculates_exhausted_key_capacity_for_configured_models(
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

    class Db:
        def list_gemini_model_states(self, *, model_name=None):  # noqa: ANN001
            assert model_name is None
            return [
                {
                    "key_id": "acc:key-1",
                    "model_name": "gemini-a",
                    "exhausted": True,
                    "attempts_cycle": 4,
                    "success_cycle": 3,
                }
            ]

    keys = [
        GeminiKey("acc", "acc:key-1", "secret-1", "secr...et-1"),
        GeminiKey("acc", "acc:key-2", "secret-2", "secr...et-2"),
    ]
    manager = GeminiRuntimeManager(Db(), task_id=None, panel_id="library")
    monkeypatch.setattr("app.gemini_runtime._utc_now", lambda: now)
    monkeypatch.setattr(manager, "_sync_key_registry", lambda: keys)
    monkeypatch.setattr(
        manager,
        "_ensure_cycle",
        lambda _now: {
            "cycle_label": "2026-08-27",
            "pause_until": None,
            "last_pause_reason": None,
            "blackout_override_until": None,
        },
    )
    monkeypatch.setattr(
        manager,
        "_clear_elapsed_pause_if_needed",
        lambda control, _now: control,
    )
    monkeypatch.setattr(
        manager,
        "_blackout_window",
        lambda _now: {
            "active": False,
            "start_utc": "2026-08-28T06:00:00+00:00",
            "end_utc": "2026-08-28T08:00:00+00:00",
            "reset_utc": "2026-08-28T07:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        "app.gemini_runtime.load_configured_gemini_model_names",
        lambda: ["gemini-a", "gemini-b"],
    )

    snapshot = manager.snapshot()

    usage = {item["model_name"]: item for item in snapshot["model_usage"]}
    assert usage["gemini-a"]["usage_percent"] == 50
    assert usage["gemini-a"]["exhausted_keys"] == 1
    assert usage["gemini-a"]["available_keys"] == 1
    assert usage["gemini-b"]["usage_percent"] == 0
    assert [
        model["model_name"]
        for model in snapshot["accounts"][0]["keys"][1]["models"]
    ] == ["gemini-a", "gemini-b"]
