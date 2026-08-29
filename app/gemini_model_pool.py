"""Reusable ordered model fallback on top of the shared Gemini key manager."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Collection, Generic, Sequence, TypeVar

from app.gemini_runtime import (
    GeminiAllKeysExhaustedError,
    GeminiQuotaExceededError,
    GeminiRequestRejectedError,
    GeminiRequestTimeoutError,
    GeminiResponseValidationError,
    GeminiRuntimeError,
    GeminiRuntimeManager,
    GeminiServerPauseError,
    GeminiStopRequestedError,
    GeminiTransportError,
)


T = TypeVar("T")


class GeminiModelResponseError(GeminiResponseValidationError):
    """A model returned content that does not satisfy the requested contract."""


class GeminiModelPoolError(RuntimeError):
    """Base ordered model-pool error."""


class GeminiModelPoolExhaustedError(GeminiModelPoolError):
    """Every configured model has produced a content-level failure."""


class GeminiModelPoolUnavailableError(GeminiModelPoolError):
    """At least one required model could not run because no key was available."""

    def __init__(self, unavailable_models: Sequence[str]) -> None:
        self.unavailable_models = tuple(unavailable_models)
        joined = ", ".join(self.unavailable_models)
        super().__init__(f"Gemini models unavailable: {joined}")


class GeminiModelPoolOperationalError(GeminiModelPoolError):
    """Gemini remained unavailable after its bounded server-error retry."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        self.retryable = bool(retryable)
        super().__init__(message)


@dataclass(frozen=True)
class _ParsedResponse(Generic[T]):
    value: T


@dataclass(frozen=True)
class _ModelAttempt(Generic[T]):
    """One model attempt classified independently from pool scheduling."""

    outcome: str
    value: T | None = None
    error: GeminiRuntimeError | GeminiModelResponseError | None = None


@dataclass(frozen=True)
class GeminiModelPoolResult(Generic[T]):
    """Validated response with the model that produced it."""

    model_name: str
    value: T
    unavailable_models: tuple[str, ...]


def _attempt_model(
    *,
    manager: GeminiRuntimeManager,
    model_name: str,
    request: Callable[[str, str, Any], Any],
    parse: Callable[[Any], T],
    run_id: int | None,
) -> _ModelAttempt[T]:
    """Run and classify one request using the shared Gemini error contract."""
    try:
        parsed_response = manager.run_with_key(
            model_name=model_name,
            call=lambda key, lease: _ParsedResponse(
                parse(request(model_name, key, lease))
            ),
            run_id=run_id,
            max_attempts=1,
        )
        value = (
            parsed_response.value
            if isinstance(parsed_response, _ParsedResponse)
            else parse(parsed_response)
        )
    except GeminiStopRequestedError:
        raise
    except GeminiQuotaExceededError as exc:
        return _ModelAttempt(outcome="quota", error=exc)
    except GeminiAllKeysExhaustedError as exc:
        return _ModelAttempt(outcome="unavailable", error=exc)
    except GeminiServerPauseError as exc:
        return _ModelAttempt(outcome="server_pause", error=exc)
    except GeminiTransportError as exc:
        return _ModelAttempt(outcome="transport", error=exc)
    except GeminiRequestRejectedError as exc:
        return _ModelAttempt(outcome="request_rejected", error=exc)
    except GeminiRequestTimeoutError as exc:
        return _ModelAttempt(outcome="timeout", error=exc)
    except GeminiModelResponseError as exc:
        return _ModelAttempt(outcome="response", error=exc)
    except GeminiRuntimeError as exc:
        return _ModelAttempt(outcome="runtime", error=exc)
    return _ModelAttempt(outcome="success", value=value)


def run_ordered_model_pool(
    *,
    manager: GeminiRuntimeManager,
    models: Sequence[str],
    request: Callable[[str, str, Any], Any],
    parse: Callable[[Any], T],
    record_failure: Callable[[str, str, str], None],
    run_id: int | None,
    already_attempted: Collection[str] = (),
) -> GeminiModelPoolResult[T]:
    """Try each model once while allowing key rotation and one 5xx retry."""
    ordered = tuple(dict.fromkeys(str(model).strip() for model in models if str(model).strip()))
    if not ordered:
        raise ValueError("Gemini model pool must not be empty")

    failed = {str(model) for model in already_attempted}
    unavailable: list[str] = []
    paused: list[tuple[str, GeminiServerPauseError]] = []

    for model_name in ordered:
        if model_name in failed:
            continue
        transport_retries = 0
        while True:
            attempt = _attempt_model(
                manager=manager,
                model_name=model_name,
                request=request,
                parse=parse,
                run_id=run_id,
            )
            if attempt.outcome == "quota":
                # The key was exhausted, not the model or document.
                continue
            if attempt.outcome == "unavailable":
                unavailable.append(model_name)
                break
            if attempt.outcome == "server_pause":
                assert isinstance(attempt.error, GeminiServerPauseError)
                paused.append((model_name, attempt.error))
                break
            if attempt.outcome == "transport":
                if transport_retries == 0:
                    transport_retries += 1
                    continue
                raise GeminiModelPoolOperationalError(
                    str(attempt.error), retryable=True
                ) from attempt.error
            if attempt.outcome == "request_rejected":
                raise GeminiModelPoolOperationalError(
                    str(attempt.error)
                ) from attempt.error
            if attempt.outcome == "timeout":
                record_failure(model_name, "timeout", str(attempt.error))
                failed.add(model_name)
                break
            if attempt.outcome == "response":
                record_failure(model_name, "response", str(attempt.error))
                failed.add(model_name)
                break
            if attempt.outcome == "runtime":
                raise GeminiModelPoolOperationalError(
                    str(attempt.error)
                ) from attempt.error
            assert attempt.outcome == "success"
            return GeminiModelPoolResult(
                model_name=model_name,
                value=attempt.value,
                unavailable_models=tuple(unavailable),
            )

    # Only wait when no other configured model could serve the item. Each model
    # that returned 5xx receives exactly one later attempt.
    for model_name, pause_error in paused:
        pause_until = pause_error.pause_until
        if pause_until is not None:
            manager._sleep_until(pause_until)  # shared stop-aware wait boundary
        while True:
            attempt = _attempt_model(
                manager=manager,
                model_name=model_name,
                request=request,
                parse=parse,
                run_id=run_id,
            )
            if attempt.outcome == "quota":
                continue
            if attempt.outcome == "unavailable":
                if model_name not in unavailable:
                    unavailable.append(model_name)
                break
            if attempt.outcome in {"server_pause", "transport"}:
                raise GeminiModelPoolOperationalError(
                    str(attempt.error), retryable=True
                ) from attempt.error
            if attempt.outcome == "request_rejected":
                raise GeminiModelPoolOperationalError(
                    str(attempt.error)
                ) from attempt.error
            if attempt.outcome in {"timeout", "response"}:
                record_failure(model_name, attempt.outcome, str(attempt.error))
                failed.add(model_name)
                break
            if attempt.outcome == "runtime":
                raise GeminiModelPoolOperationalError(
                    str(attempt.error)
                ) from attempt.error
            assert attempt.outcome == "success"
            return GeminiModelPoolResult(
                model_name=model_name,
                value=attempt.value,
                unavailable_models=tuple(unavailable),
            )

    if all(model in failed for model in ordered):
        raise GeminiModelPoolExhaustedError(
            "All configured Gemini models failed response validation"
        )
    raise GeminiModelPoolUnavailableError(unavailable)


__all__ = [
    "GeminiModelPoolExhaustedError",
    "GeminiModelPoolOperationalError",
    "GeminiModelPoolResult",
    "GeminiModelPoolUnavailableError",
    "GeminiModelResponseError",
    "run_ordered_model_pool",
]
