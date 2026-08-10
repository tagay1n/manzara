"""Ordered Gemini model-pool behavior shared by document tasks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.gemini_model_pool import (
    GeminiModelPoolOperationalError,
    GeminiModelPoolUnavailableError,
    GeminiModelResponseError,
    run_ordered_model_pool,
)
from app.gemini_runtime import (
    GeminiAllKeysExhaustedError,
    GeminiQuotaExceededError,
    GeminiRequestTimeoutError,
    GeminiRuntimeError,
    GeminiServerPauseError,
)


class _Manager:
    def __init__(self, actions: dict[str, list[object]]) -> None:
        self.actions = {key: list(value) for key, value in actions.items()}
        self.calls: list[str] = []

    def run_with_key(self, *, model_name, call, run_id, max_attempts):  # noqa: ANN001
        assert max_attempts == 1
        self.calls.append(model_name)
        action = self.actions[model_name].pop(0)
        if isinstance(action, Exception):
            raise action
        return call("key", SimpleNamespace(model_name=model_name)) if action == "call" else action


def test_model_pool_records_bad_response_then_uses_next_model() -> None:
    manager = _Manager({"first": ["bad"], "second": ["good"]})
    failures: list[tuple[str, str, str]] = []

    result = run_ordered_model_pool(
        manager=manager,
        models=["first", "second"],
        run_id=7,
        request=lambda model, _key, _lease: manager.actions[model],
        parse=lambda raw: (
            (_ for _ in ()).throw(GeminiModelResponseError("malformed"))
            if raw == "bad"
            else {"name": raw}
        ),
        record_failure=lambda model, kind, error: failures.append((model, kind, error)),
    )

    assert result.model_name == "second"
    assert result.value == {"name": "good"}
    assert manager.calls == ["first", "second"]
    assert failures == [("first", "response", "malformed")]


def test_model_pool_retries_same_model_after_quota_key_rotation() -> None:
    manager = _Manager(
        {
            "first": [
                GeminiQuotaExceededError("key exhausted"),
                "good",
            ]
        }
    )

    result = run_ordered_model_pool(
        manager=manager,
        models=["first"],
        run_id=8,
        request=lambda _model, _key, _lease: None,
        parse=lambda raw: raw,
        record_failure=lambda *_args: None,
    )

    assert result.value == "good"
    assert manager.calls == ["first", "first"]


def test_model_pool_does_not_mark_unavailable_model_as_content_failure() -> None:
    manager = _Manager(
        {
            "first": [GeminiAllKeysExhaustedError("none")],
            "second": [GeminiAllKeysExhaustedError("none")],
        }
    )
    failures: list[str] = []

    with pytest.raises(GeminiModelPoolUnavailableError) as exc_info:
        run_ordered_model_pool(
            manager=manager,
            models=["first", "second"],
            run_id=9,
            request=lambda _model, _key, _lease: None,
            parse=lambda raw: raw,
            record_failure=lambda model, _kind, _error: failures.append(model),
        )

    assert exc_info.value.unavailable_models == ("first", "second")
    assert failures == []


def test_model_pool_skips_persisted_content_failures() -> None:
    manager = _Manager({"second": ["good"]})

    result = run_ordered_model_pool(
        manager=manager,
        models=["first", "second"],
        already_attempted={"first"},
        run_id=10,
        request=lambda _model, _key, _lease: None,
        parse=lambda raw: raw,
        record_failure=lambda *_args: None,
    )

    assert result.model_name == "second"
    assert manager.calls == ["second"]


def test_model_timeout_falls_through_but_unknown_runtime_error_is_operational() -> None:
    manager = _Manager(
        {"first": [GeminiRequestTimeoutError("timeout")], "second": ["good"]}
    )
    failures: list[tuple[str, str]] = []

    result = run_ordered_model_pool(
        manager=manager,
        models=["first", "second"],
        run_id=11,
        request=lambda _model, _key, _lease: None,
        parse=lambda raw: raw,
        record_failure=lambda model, kind, _error: failures.append((model, kind)),
    )
    assert result.model_name == "second"
    assert failures == [("first", "timeout")]

    manager = _Manager({"first": [GeminiRuntimeError("bad credentials")]})
    with pytest.raises(GeminiModelPoolOperationalError, match="bad credentials"):
        run_ordered_model_pool(
            manager=manager,
            models=["first"],
            run_id=12,
            request=lambda _model, _key, _lease: None,
            parse=lambda raw: raw,
            record_failure=lambda *_args: None,
        )


def test_repeated_server_pause_is_retryable_operational_error() -> None:
    manager = _Manager(
        {
            "first": [
                GeminiServerPauseError("paused"),
                GeminiServerPauseError("still paused"),
            ]
        }
    )

    with pytest.raises(GeminiModelPoolOperationalError) as exc_info:
        run_ordered_model_pool(
            manager=manager,
            models=["first"],
            run_id=13,
            request=lambda _model, _key, _lease: None,
            parse=lambda raw: raw,
            record_failure=lambda *_args: None,
        )

    assert exc_info.value.retryable is True
