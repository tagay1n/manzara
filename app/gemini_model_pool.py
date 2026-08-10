"""Reusable ordered model fallback on top of the shared Gemini key manager."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Collection, Generic, Sequence, TypeVar

from app.gemini_runtime import (
    GeminiAllKeysExhaustedError,
    GeminiQuotaExceededError,
    GeminiRequestRejectedError,
    GeminiRequestTimeoutError,
    GeminiRuntimeError,
    GeminiRuntimeManager,
    GeminiServerPauseError,
    GeminiStopRequestedError,
)


T = TypeVar("T")


class GeminiModelResponseError(ValueError):
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
class GeminiModelPoolResult(Generic[T]):
    """Validated response with the model that produced it."""

    model_name: str
    value: T
    unavailable_models: tuple[str, ...]


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

    for model_name in ordered:
        if model_name in failed:
            continue
        server_retries = 0
        while True:
            try:
                raw = manager.run_with_key(
                    model_name=model_name,
                    call=lambda key, lease: request(model_name, key, lease),
                    run_id=run_id,
                    max_attempts=1,
                )
            except GeminiQuotaExceededError:
                # The key was exhausted, not the model or document.
                continue
            except GeminiAllKeysExhaustedError:
                unavailable.append(model_name)
                break
            except GeminiServerPauseError as exc:
                if server_retries == 0:
                    server_retries += 1
                    continue
                raise GeminiModelPoolOperationalError(
                    str(exc), retryable=True
                ) from exc
            except GeminiStopRequestedError:
                raise
            except GeminiRequestRejectedError as exc:
                record_failure(model_name, "request", str(exc))
                failed.add(model_name)
                break
            except GeminiRequestTimeoutError as exc:
                record_failure(model_name, "timeout", str(exc))
                failed.add(model_name)
                break
            except GeminiRuntimeError as exc:
                raise GeminiModelPoolOperationalError(str(exc)) from exc

            try:
                value = parse(raw)
            except GeminiModelResponseError as exc:
                record_failure(model_name, "response", str(exc))
                failed.add(model_name)
                break
            return GeminiModelPoolResult(
                model_name=model_name,
                value=value,
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
