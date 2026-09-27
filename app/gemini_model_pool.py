"""Bounded item fallback on top of the shared ready-model scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
    """Base model-pool error."""


class GeminiModelPoolExhaustedError(GeminiModelPoolError):
    """Every configured model has produced a content-level failure."""


class GeminiModelPoolUnavailableError(GeminiModelPoolError):
    """At least one required model could not run because no key was available."""

    def __init__(
        self,
        unavailable_models: Sequence[str],
        *,
        retry_at: datetime | None = None,
        all_models_unavailable: bool = True,
    ) -> None:
        self.all_models_unavailable = all_models_unavailable
        self.retry_at = retry_at
        self.unavailable_models = tuple(unavailable_models)
        joined = ", ".join(self.unavailable_models)
        super().__init__(f"Gemini models unavailable: {joined}")


class GeminiModelPoolOperationalError(GeminiModelPoolError):
    """Gemini remained unavailable after its bounded server-error retry."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_at: datetime | None = None,
        model_name: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.retry_at = retry_at
        self.retryable = bool(retryable)
        super().__init__(message)


class GeminiModelPoolItemRejectedError(GeminiModelPoolOperationalError):
    """Gemini rejected one item's request without invalidating the task runtime."""


@dataclass(frozen=True)
class _ParsedResponse(Generic[T]):
    value: T


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
    yield_on_transient: bool = False,
) -> GeminiModelPoolResult[T]:
    """Use shared ready-model rotation while preserving bounded item attempts."""
    ordered = tuple(dict.fromkeys(str(m).strip() for m in models if str(m).strip()))
    if not ordered:
        raise ValueError("Gemini model pool must not be empty")
    failed = set(already_attempted)
    unavailable: list[str] = []
    quota_attempts: dict[str, int] = {}
    transient_attempts: dict[str, int] = {}
    max_quota = manager.max_quota_rotations_per_model
    while eligible := [m for m in ordered if m not in failed and m not in unavailable]:
        selected: str | None = None

        def invoke(model, key, lease):
            nonlocal selected
            selected = model
            return _ParsedResponse(parse(request(model, key, lease)))

        try:
            selected, response = manager.run_with_available_model(
                models=eligible,
                pool_models=ordered,
                call=invoke,
                run_id=run_id,
            )
            value = (
                response.value
                if isinstance(response, _ParsedResponse)
                else parse(response)
            )
        except GeminiStopRequestedError:
            raise
        except GeminiAllKeysExhaustedError as exc:
            raise GeminiModelPoolUnavailableError(
                eligible,
                retry_at=getattr(exc, "retry_at", None),
                all_models_unavailable=getattr(
                    exc, "all_models_unavailable", not failed
                ),
            ) from exc
        except (
            GeminiQuotaExceededError,
            GeminiServerPauseError,
            GeminiTransportError,
            GeminiRequestTimeoutError,
        ) as exc:
            selected = selected or getattr(exc, "model_name", None)
            if selected is None:
                raise GeminiModelPoolOperationalError(str(exc), retryable=True) from exc
            if yield_on_transient:
                raise GeminiModelPoolOperationalError(
                    str(exc),
                    retryable=True,
                    model_name=selected,
                    retry_at=getattr(exc, "pause_until", None),
                ) from exc
            if isinstance(exc, GeminiQuotaExceededError):
                quota_attempts[selected] = quota_attempts.get(selected, 0) + 1
                if quota_attempts[selected] >= max_quota:
                    unavailable.append(selected)
            elif isinstance(exc, GeminiRequestTimeoutError):
                record_failure(selected, "timeout", str(exc))
                failed.add(selected)
            else:
                transient_attempts[selected] = transient_attempts.get(selected, 0) + 1
                if transient_attempts[selected] >= 2:
                    raise GeminiModelPoolOperationalError(
                        str(exc), retryable=True
                    ) from exc
            continue
        except GeminiRequestRejectedError as exc:
            raise GeminiModelPoolItemRejectedError(str(exc)) from exc
        except GeminiModelResponseError as exc:
            selected = selected or getattr(exc, "model_name", None)
            if selected is None:
                raise
            record_failure(selected, "response", str(exc))
            failed.add(selected)
            continue
        except GeminiRuntimeError as exc:
            if yield_on_transient:
                raise
            raise GeminiModelPoolOperationalError(str(exc)) from exc
        return GeminiModelPoolResult(selected, value, tuple(unavailable))
    if all(model in failed for model in ordered):
        raise GeminiModelPoolExhaustedError(
            "All configured Gemini models failed response validation"
        )
    raise GeminiModelPoolUnavailableError(
        unavailable, all_models_unavailable=not failed
    )


__all__ = [
    "GeminiModelPoolExhaustedError",
    "GeminiModelPoolItemRejectedError",
    "GeminiModelPoolOperationalError",
    "GeminiModelPoolResult",
    "GeminiModelPoolUnavailableError",
    "GeminiModelResponseError",
    "run_ordered_model_pool",
]
