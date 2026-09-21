from __future__ import annotations

from datetime import datetime, timezone
import time

import pytest

from app.gemini_runtime import (
    GeminiLease,
    GeminiQuotaExceededError,
    GeminiRuntimeManager,
    GeminiStopRequestedError,
    GeminiTransportError,
    _classify_quota_error,
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


def test_terminated_upload_session_is_transient_despite_http_400() -> None:
    class Db:
        def __init__(self) -> None:
            self.errors = []
            self.events = []

        def mark_gemini_error(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.errors.append((args, kwargs))

        def insert_event(self, event_type, **kwargs):  # noqa: ANN001
            self.events.append((event_type, kwargs))

    class UploadTerminatedError(Exception):
        status_code = 400

    db = Db()
    manager = GeminiRuntimeManager(
        db, task_id="library.metadata_evaluate", panel_id="metadata"
    )
    lease = GeminiLease("account", "key-id", "secret", "secr...cret", "model")

    with pytest.raises(GeminiTransportError, match="upload session terminated"):
        manager._handle_error(
            lease=lease,
            error=UploadTerminatedError("Upload has already been terminated."),
            run_id=55,
        )

    assert db.errors
    event_type, event = db.events[-1]
    assert event_type == "gemini.request.transport_error"
    assert event["payload"]["reason"] == "upload_session_terminated"
    assert event["payload"]["status_code"] == 400


class _QuotaError(Exception):
    status_code = 429

    def __init__(self, details):  # noqa: ANN001
        self.details = details
        super().__init__("429 RESOURCE_EXHAUSTED")


def test_quota_classifier_only_treats_explicit_daily_limits_as_exhaustion() -> None:
    daily = _QuotaError(
        {
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaMetric": (
                                    "generativelanguage.googleapis.com/"
                                    "generate_requests_per_model_per_day"
                                )
                            }
                        ],
                    }
                ]
            }
        }
    )
    temporary = _QuotaError(
        {
            "error": {
                "message": "Resource has been exhausted (e.g. check quota).",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "37s",
                    }
                ],
            }
        }
    )

    assert _classify_quota_error(daily).daily is True
    disposition = _classify_quota_error(temporary)
    assert disposition.daily is False
    assert disposition.retry_after_seconds == 37


def test_generic_429_cools_quota_domain_without_exhausting_key(monkeypatch) -> None:
    now = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)

    class Db:
        def __init__(self) -> None:
            self.errors = []
            self.cooldowns = []
            self.events = []

        def mark_gemini_error(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.errors.append((args, kwargs))

        def get_gemini_quota_domain_model_state(self, *_args):  # noqa: ANN002
            return None

        def set_gemini_quota_domain_model_cooldown(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.cooldowns.append((args, kwargs))

        def insert_event(self, event_type, **kwargs):  # noqa: ANN001
            self.events.append((event_type, kwargs))

    db = Db()
    manager = GeminiRuntimeManager(db, task_id="task", panel_id="library")
    lease = GeminiLease(
        "account", "key-id", "secret", "masked", "model", quota_domain_id="project"
    )
    monkeypatch.setattr("app.gemini_runtime._utc_now", lambda: now)

    with pytest.raises(GeminiQuotaExceededError):
        manager._handle_error(
            lease=lease,
            error=_QuotaError({"error": {"message": "Resource exhausted"}}),
            run_id=3,
        )

    assert db.errors[0][1]["exhausted"] is False
    assert db.cooldowns[0][0] == ("project", "model")
    assert db.cooldowns[0][1]["failure_count"] == 1
    assert db.cooldowns[0][1]["cooldown_until"] == "2026-09-13T10:01:00+00:00"
    assert db.events[-1][0] == "gemini.quota.cooldown.started"


def test_three_distinct_generic_429_domains_open_shared_model_circuit(
    monkeypatch,
) -> None:
    now = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)

    class Db:
        def __init__(self) -> None:
            self.pauses = []
            self.events = []

        def mark_gemini_error(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return None

        def get_gemini_quota_domain_model_state(self, *_args):  # noqa: ANN002
            return None

        def set_gemini_quota_domain_model_cooldown(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return None

        def record_gemini_generic_quota_signal(self, **_kwargs):  # noqa: ANN003
            return 3

        def set_gemini_model_pause(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.pauses.append((args, kwargs))

        def insert_event(self, event_type, **kwargs):  # noqa: ANN001
            self.events.append((event_type, kwargs))

    db = Db()
    manager = GeminiRuntimeManager(db, task_id="task", panel_id="library")
    lease = GeminiLease(
        "account", "key-id", "secret", "masked", "model", quota_domain_id="project-c"
    )
    monkeypatch.setattr("app.gemini_runtime._utc_now", lambda: now)

    with pytest.raises(GeminiQuotaExceededError):
        manager._handle_error(
            lease=lease,
            error=_QuotaError({"error": {"message": "Resource exhausted"}}),
            run_id=3,
        )

    assert db.pauses[0][0] == (
        "model",
        "2026-09-13T10:01:00+00:00",
    )
    assert db.pauses[0][1] == {"reason": "generic_429_circuit_breaker"}
    assert any(event[0] == "gemini.model.quota_circuit_opened" for event in db.events)


def test_global_request_gate_waits_before_reserving_another_physical_call(
    monkeypatch,
) -> None:
    now = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)

    class Db:
        def __init__(self) -> None:
            self.claims = 0
            self.events = []

        def try_claim_gemini_request_slot(self, **_kwargs):  # noqa: ANN003
            self.claims += 1
            if self.claims == 1:
                return {
                    "claimed": False,
                    "oldest_request_at": "2026-09-13T09:59:30+00:00",
                    "requests_in_window": 10,
                }
            return {
                "claimed": True,
                "oldest_request_at": None,
                "requests_in_window": 10,
            }

        def insert_event(self, event_type, **kwargs):  # noqa: ANN001
            self.events.append((event_type, kwargs))

    db = Db()
    manager = GeminiRuntimeManager(db, task_id="task", panel_id="library")
    waits = []
    monkeypatch.setattr("app.gemini_runtime._utc_now", lambda: now)
    monkeypatch.setattr(manager, "_sleep_until", lambda wait_until: waits.append(wait_until))

    manager._reserve_request_capacity(model_name="model", run_id=7)

    assert db.claims == 2
    assert waits == [datetime(2026, 9, 13, 10, 0, 30, tzinfo=timezone.utc)]
    assert [event[0] for event in db.events] == ["gemini.rate_limit.waiting"]


def test_explicit_daily_429_exhausts_the_whole_quota_domain_model() -> None:
    class Db:
        def __init__(self) -> None:
            self.exhaustions = []
            self.events = []

        def mark_gemini_quota_domain_model_exhausted(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.exhaustions.append((args, kwargs))
            return 4

        def insert_event(self, event_type, **kwargs):  # noqa: ANN001
            self.events.append((event_type, kwargs))

    db = Db()
    manager = GeminiRuntimeManager(db, task_id="task", panel_id="library")
    lease = GeminiLease(
        "account", "key-id", "secret", "masked", "model", quota_domain_id="project"
    )
    error = _QuotaError(
        {
            "error": {
                "details": [
                    {
                        "quotaMetric": "generate_requests_per_model_per_day",
                    }
                ]
            }
        }
    )

    with pytest.raises(GeminiQuotaExceededError):
        manager._handle_error(lease=lease, error=error, run_id=4)

    assert db.exhaustions[0][0] == ("project", "model")
    assert db.events[-1][0] == "gemini.key.exhausted"
    assert (
        db.events[-1][1]["payload"]["quota_scope"]
        == "quota_domain_model_daily"
    )
    assert db.events[-1][1]["payload"]["rows_changed"] == 4


def test_candidate_reports_all_accounts_cooling_down() -> None:
    now = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)

    class Db:
        def list_gemini_model_states(self, *, model_name=None):  # noqa: ANN001
            return [
                {"key_id": "a:key", "model_name": model_name, "exhausted": False},
                {"key_id": "b:key", "model_name": model_name, "exhausted": False},
            ]

        def list_gemini_quota_domain_model_states(self, *, model_name=None):  # noqa: ANN001
            return [
                {
                    "quota_domain_id": "a:key",
                    "model_name": model_name,
                    "cooldown_until": "2026-09-13T10:04:00+00:00",
                },
                {
                    "quota_domain_id": "b:key",
                    "model_name": model_name,
                    "cooldown_until": "2026-09-13T10:02:00+00:00",
                },
            ]

    manager = GeminiRuntimeManager(Db(), task_id="task", panel_id="library")
    keys = [
        GeminiKey("a", "a:key", "secret-a", "a***"),
        GeminiKey("b", "b:key", "secret-b", "b***"),
    ]

    decision = manager._pick_candidate(keys=keys, model_name="model", now_utc=now)

    assert decision["type"] == "quota_cooldown"
    assert decision["wait_until"] == datetime(
        2026, 9, 13, 10, 2, tzinfo=timezone.utc
    )


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

        def ensure_gemini_runtime_cycle(self, _cycle_label):  # noqa: ANN001
            return {**self.control, "rolled": False}

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

        def get_gemini_snapshot_metadata(self):
            return {"account_leases": [], "model_runtime": []}

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


def test_long_request_renews_and_releases_account_lease(monkeypatch) -> None:
    class Db:
        def __init__(self) -> None:
            self.renewals = 0
            self.releases = 0
            self.quota_signal_clears = 0

        def renew_gemini_account_lease(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            self.renewals += 1
            return True

        def release_gemini_account_lease(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            self.releases += 1
            return True

        def mark_gemini_success(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return None

        def clear_gemini_generic_quota_signals(self, _model_name):  # noqa: ANN001
            self.quota_signal_clears += 1
            return 1

        def insert_event(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            return None

    db = Db()
    manager = GeminiRuntimeManager(db, task_id="task", panel_id="library")
    lease = GeminiLease("account", "key", "secret", "masked", "model", "token")
    monkeypatch.setattr(manager, "acquire_key", lambda **_kwargs: lease)
    monkeypatch.setattr("app.gemini_runtime._ACCOUNT_LEASE_HEARTBEAT_SECONDS", 0.01)

    result = manager.run_with_key(
        model_name="model",
        call=lambda *_args: (time.sleep(0.04), "ok")[1],
    )

    assert result == "ok"
    assert db.renewals >= 2
    assert db.releases == 1
    assert db.quota_signal_clears == 1
