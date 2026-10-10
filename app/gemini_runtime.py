"""Unified Gemini key/runtime coordination for all Manzara tasks."""

from __future__ import annotations

import errno
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from app.db import Database
from app.gemini_config import (
    GeminiKey,
    load_gemini_keys,
    load_gemini_runtime_limits,
)
from app.gemini_pacing import GeminiPacingPolicy, PacingOutcome
from app.repositories.gemini_pacing import GeminiPacingAdmissionLost
from app.task_runtime.logging import log_message

_PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
_UTC = timezone.utc


_GENERATION_START: ContextVar[Callable[[], None] | None] = ContextVar(
    "gemini_generation_start", default=None,
)


def record_gemini_generation_start() -> None:
    """Record the physical generation start in the current leased request."""
    callback = _GENERATION_START.get()
    if callback is not None:
        callback()


class GeminiRuntimeError(RuntimeError):
    """Base runtime error for Gemini coordination."""


class GeminiQuotaExceededError(GeminiRuntimeError):
    """Raised when Gemini returns 429 for a quota domain and model."""


class GeminiAllKeysExhaustedError(GeminiRuntimeError):
    """Raised when no non-exhausted keys remain for the eligible models."""

    def __init__(self, message: str, *, all_models_unavailable: bool = True, retry_at: datetime | None = None):
        self.retry_at = retry_at
        self.all_models_unavailable = all_models_unavailable
        super().__init__(message)


class GeminiQuotaCooldownError(GeminiAllKeysExhaustedError):
    """Raised when every otherwise-usable quota domain is cooling down."""

    def __init__(self, message: str, *, retry_at: datetime | None = None):
        super().__init__(message, retry_at=retry_at)


class GeminiServerPauseError(GeminiRuntimeError):
    """Raised when one Gemini model is temporarily paused after a 5xx."""

    def __init__(self, message: str, *, model_name: str | None = None, pause_until: datetime | None = None):
        self.model_name = model_name
        self.pause_until = pause_until
        super().__init__(message)


class GeminiRequestRejectedError(GeminiRuntimeError):
    """Raised when Gemini rejects one request payload (e.g. HTTP 400)."""


class GeminiRequestTimeoutError(GeminiRuntimeError):
    """Raised when one model request exceeds its response deadline."""


class GeminiTransportError(GeminiRuntimeError):
    """Raised for a transient network failure without an HTTP response."""


class GeminiStopRequestedError(GeminiRuntimeError):
    """Raised when a task requests graceful stop while waiting for Gemini."""


class GeminiResponseValidationError(ValueError):
    """Response content failed validation and must not affect key health."""


@dataclass(frozen=True)
class GeminiLease:
    """Reserved key context for one Gemini request attempt."""

    account_id: str
    key_id: str
    key_value: str
    masked_key: str
    model_name: str
    project_lease_token: str
    quota_domain_id: str
    pacing_epoch: int = 0
    pacing_probe: bool = False


@dataclass(frozen=True)
class _QuotaDisposition:
    daily: bool
    retry_after_seconds: int | None = None


def _walk_error_details(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_error_details(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_error_details(item)
    elif value is not None:
        yield str(value)


def _parse_retry_delay(value: Any) -> int | None:
    if isinstance(value, dict):
        try:
            seconds = float(value.get("seconds") or 0)
            nanos = float(value.get("nanos") or 0)
        except (TypeError, ValueError):
            return None
        return max(1, int(seconds + nanos / 1_000_000_000 + 0.999))
    raw = str(value or "").strip().casefold()
    if raw.endswith("s"):
        raw = raw[:-1]
    try:
        return max(1, int(float(raw) + 0.999))
    except ValueError:
        return None


def _find_retry_delay(value: Any) -> int | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in {"retrydelay", "retry_delay"}:
                parsed = _parse_retry_delay(item)
                if parsed is not None:
                    return parsed
            nested = _find_retry_delay(item)
            if nested is not None:
                return nested
    elif isinstance(value, (list, tuple)):
        for item in value:
            nested = _find_retry_delay(item)
            if nested is not None:
                return nested
    return None


def _classify_quota_error(error: Exception) -> _QuotaDisposition:
    """Only explicit per-day quota evidence warrants day-long exhaustion."""
    details = getattr(error, "details", None)
    searchable = " ".join(
        [str(error), *_walk_error_details(details)]
    ).casefold()
    daily_markers = (
        "per_day",
        "per-day",
        "per day",
        "requestsperday",
        "tokensperday",
        "daily quota",
    )
    retry_after = _find_retry_delay(details)
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if retry_after is None and headers is not None:
        retry_after = _parse_retry_delay(headers.get("retry-after"))
    return _QuotaDisposition(
        daily=any(marker in searchable for marker in daily_markers),
        retry_after_seconds=retry_after,
    )


def _utc_now() -> datetime:
    return datetime.now(_UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(_UTC).isoformat()


def _parse_ts(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)


def _extract_status_code(error: Exception) -> Optional[int]:
    for attr in ("status_code", "code"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    text = str(error)
    for code in (429, 500, 501, 502, 503, 504):
        if f"{code}" in text:
            return code
    return None


def _is_timeout_error(error: Exception) -> bool:
    if isinstance(error, TimeoutError):
        return True
    name = type(error).__name__.casefold()
    message = str(error).casefold()
    return "timeout" in name or "timed out" in message or "deadline exceeded" in message


def _is_transport_error(error: Exception) -> bool:
    retryable_errno = {
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ENETDOWN,
        errno.ENETRESET,
        errno.ENETUNREACH,
        errno.EPIPE,
    }
    retryable_names = {
        "connecterror",
        "connectionerror",
        "connectionreseterror",
        "networkerror",
        "readerror",
        "remoteprotocolerror",
        "writeerror",
    }
    retryable_messages = (
        "broken pipe",
        "connection aborted",
        "connection reset",
        "remote end closed connection",
        "server disconnected",
    )
    current: Optional[BaseException] = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionError):
            return True
        if getattr(current, "errno", None) in retryable_errno:
            return True
        if type(current).__name__.casefold() in retryable_names:
            return True
        message = str(current).casefold()
        if any(marker in message for marker in retryable_messages):
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_terminated_upload_error(error: Exception) -> bool:
    """Return whether Gemini invalidated a resumable upload session."""
    return "upload has already been terminated" in str(error).casefold()


class GeminiRuntimeManager:
    """Shared Gemini key allocator with local runtime state and request coordination."""

    def __init__(
        self,
        db: Database,
        *,
        task_id: Optional[str],
        should_stop: Optional[Callable[[], bool]] = None,
        pacing_policy: GeminiPacingPolicy | None = None,
    ):
        self.db = db
        self.task_id = task_id
        self.should_stop = should_stop or (lambda: False)
        self.pacing_policy = pacing_policy
        self._configured_keys: Optional[List[GeminiKey]] = None
        self._prepared_models: set[str] = set()
        self._limits = load_gemini_runtime_limits()

    @property
    def max_quota_rotations_per_model(self) -> int:
        return self._limits.max_quota_rotations_per_model

    def _set_wait(self, run_id: int | None, payload: dict) -> None:
        if run_id is not None:
            self.db.set_run_provider_wait(run_id, payload)

    def _sync_key_registry(self) -> List[GeminiKey]:
        if self._configured_keys is not None:
            return list(self._configured_keys)
        keys = load_gemini_keys()
        self.db.upsert_gemini_keys(
            [
                {
                    "key_id": item.key_id,
                    "account_id": item.account_id,
                    "masked_key": item.masked_key,
                    "quota_domain_id": item.quota_domain_id,
                }
                for item in keys
            ]
        )
        self._configured_keys = list(keys)
        return list(keys)

    @staticmethod
    def _cycle_label(now_utc: datetime) -> str:
        return now_utc.astimezone(_PACIFIC_TZ).date().isoformat()

    def _blackout_window(self, now_utc: datetime) -> Dict[str, Any]:
        pt_now = now_utc.astimezone(_PACIFIC_TZ)
        midnight_today = datetime(
            pt_now.year, pt_now.month, pt_now.day, tzinfo=_PACIFIC_TZ
        )
        midnight_next = midnight_today + timedelta(days=1)

        prev_start = midnight_today - timedelta(seconds=self._limits.reset_blackout_seconds)
        prev_end = midnight_today + timedelta(seconds=self._limits.reset_blackout_seconds)
        next_start = midnight_next - timedelta(seconds=self._limits.reset_blackout_seconds)
        next_end = midnight_next + timedelta(seconds=self._limits.reset_blackout_seconds)

        active = False
        reset_at = midnight_next
        start = next_start
        end = next_end

        if prev_start <= pt_now < prev_end:
            active = True
            reset_at = midnight_today
            start = prev_start
            end = prev_end
        elif next_start <= pt_now < next_end:
            active = True
            reset_at = midnight_next
            start = next_start
            end = next_end

        return {
            "active": active,
            "start_utc": _iso_utc(start),
            "end_utc": _iso_utc(end),
            "reset_utc": _iso_utc(reset_at),
            "wait_until_utc": _iso_utc(end) if active else None,
        }

    def _ensure_cycle(self, now_utc: datetime) -> None:
        self.db.ensure_gemini_runtime_cycle(self._cycle_label(now_utc))

    def _ensure_model_rows(self, keys: List[GeminiKey], model_name: str) -> None:
        if model_name in self._prepared_models:
            return
        self.db.ensure_gemini_model_states(
            [key.key_id for key in keys], model_name
        )
        self._prepared_models.add(model_name)

    def _sleep_until(self, wait_until: Optional[datetime]) -> None:
        if wait_until is None:
            if self.should_stop():
                raise GeminiStopRequestedError(
                    "Gemini wait interrupted by graceful stop"
                )
            time.sleep(self._limits.capacity_poll_seconds)
            return
        while True:
            if self.should_stop():
                raise GeminiStopRequestedError(
                    "Gemini wait interrupted by graceful stop"
                )
            now_utc = _utc_now()
            remaining = (wait_until - now_utc).total_seconds()
            if remaining <= 0:
                return
            time.sleep(min(float(remaining), self._limits.max_wait_slice_seconds))


    def acquire_available_key(
        self,
        *,
        models: Sequence[str],
        pool_models: Sequence[str] | None = None,
        run_id: Optional[int] = None,
    ) -> GeminiLease:
        """Wait for ready project capacity and any opt-in run pacing gate."""
        models = tuple(dict.fromkeys(models))
        if not models:
            raise ValueError("Gemini model pool must not be empty")
        last_wait = None
        while True:
            if self.should_stop():
                raise GeminiStopRequestedError(
                    "Gemini request skipped after graceful stop"
                )
            now = _utc_now()
            keys = self._sync_key_registry()
            if not keys:
                raise GeminiAllKeysExhaustedError("No Gemini keys configured")
            self._ensure_cycle(now)
            blackout = self._blackout_window(now)
            if blackout["active"]:
                self._set_wait(run_id, {"mode": "daily reset", "wait_until": blackout["wait_until_utc"]})
                self._sleep_until(_parse_ts(blackout["wait_until_utc"]))
                continue
            for model in pool_models or models:
                self._ensure_model_rows(keys, model)
            cooldown = _iso_utc(now + timedelta(seconds=self._limits.project_model_spacing_seconds))
            claim_args = dict(
                key_ids=[key.key_id for key in keys],
                now_ts=_iso_utc(now),
                cooldown_until=cooldown,
                expires_at=_iso_utc(
                    now + timedelta(seconds=self._limits.project_lease_ttl_seconds)
                ),
                lease_token=uuid.uuid4().hex,
                task_id=self.task_id,
                run_id=run_id,
            )
            if self.pacing_policy is not None:
                claim_args["pacing_policy"] = self.pacing_policy
            decision = self.db.claim_gemini_ready_request(models=models, **claim_args)
            if decision.get("wait_reason") == "pacing":
                wait_until = _parse_ts(decision["retry_at"])
                self._set_wait(run_id, {"mode": "pacing", "wait_until": decision["retry_at"]})
                self._sleep_until(min(wait_until, now + timedelta(seconds=self._limits.capacity_poll_seconds)))
                continue
            if "key_id" not in decision:
                retry_at = _parse_ts(decision.get("retry_at"))
                full = decision
                if pool_models and set(pool_models) != set(models):
                    full = self.db.claim_gemini_ready_request(
                        models=pool_models,
                        reserve=False,
                        **claim_args,
                    )
                if "key_id" in full or retry_at is None:
                    error = (
                        GeminiQuotaCooldownError(
                            "Eligible Gemini models unavailable for this item",
                            retry_at=retry_at,
                        )
                        if retry_at
                        else GeminiAllKeysExhaustedError(
                            "All eligible Gemini projects/models exhausted"
                        )
                    )
                    error.all_models_unavailable = (
                        "key_id" not in full and full.get("retry_at") is None
                    )
                    raise error
                wait_until = _parse_ts(full.get("retry_at")) or retry_at
                if last_wait != wait_until:
                    self._set_wait(run_id, {"mode": "capacity", "wait_until": _iso_utc(wait_until)})
                    log_message(
                        f"gemini runtime: waiting models={','.join(models)} until={_iso_utc(wait_until)}",
                    )
                    last_wait = wait_until
                # A peer may release its project before the lease expiry or clear
                # a pause. Recheck SQLite without sleeping through that capacity.
                self._sleep_until(min(wait_until, now + timedelta(seconds=self._limits.capacity_poll_seconds)))
                continue
            key = next(key for key in keys if key.key_id == decision["key_id"])
            model = decision["model_name"]
            self._set_wait(run_id, {})
            if "pacing_snapshot" in decision:
                self._report_pacing(decision["pacing_snapshot"], run_id=run_id)
            return GeminiLease(
                account_id=key.account_id,
                key_id=key.key_id,
                key_value=key.key_value,
                masked_key=key.masked_key,
                model_name=model,
                quota_domain_id=decision["quota_domain_id"],
                project_lease_token=decision["lease_token"],
                pacing_epoch=decision.get("pacing_epoch", 0),
                pacing_probe=decision.get("pacing_probe", False),
            )

    def run_with_available_model(
        self,
        *,
        models: Sequence[str],
        call: Callable[[str, str, GeminiLease], Any],
        pool_models: Sequence[str] | None = None,
        run_id: Optional[int] = None,
    ) -> tuple[str, Any]:
        """Execute one leased request; the model pool owns bounded retries."""
        while True:
            lease = self.acquire_available_key(models=models, pool_models=pool_models, run_id=run_id)
            heartbeat_stop = threading.Event()
            heartbeat = threading.Thread(
                target=self._lease_heartbeat, args=(lease, heartbeat_stop),
                daemon=True, name=f"gemini-lease-{lease.account_id}",
            )
            generation_context = _GENERATION_START.set(lambda: self._record_generation_start(lease, run_id))
            try:
                heartbeat.start()
                result = call(lease.model_name, lease.key_value, lease)
            except GeminiPacingAdmissionLost:
                # Preparation lost admission before sending anything; reacquire.
                continue
            except GeminiResponseValidationError:
                self._record_pacing_outcome(lease, "success", run_id=run_id)
                raise
            except GeminiStopRequestedError:
                raise
            except Exception as error:
                self._record_pacing_outcome(lease, self._pacing_error_outcome(error), run_id=run_id)
                self._handle_error(lease=lease, error=error)
            else:
                self._record_pacing_outcome(lease, "success", run_id=run_id)
                self.db.clear_gemini_generic_quota_signals(lease.model_name)
                self.db.clear_gemini_quota_domain_model_state(lease.quota_domain_id, lease.model_name)
                self.db.mark_gemini_success(lease.key_id, lease.model_name, now_ts=_iso_utc(_utc_now()))
                return lease.model_name, result
            finally:
                _GENERATION_START.reset(generation_context)
                heartbeat_stop.set()
                if heartbeat.ident is not None:
                    heartbeat.join()
                self.db.release_gemini_project_lease(lease.quota_domain_id, lease.project_lease_token)

    def _record_generation_start(self, lease: GeminiLease, run_id: int | None) -> None:
        while True:
            if self.should_stop():
                raise GeminiStopRequestedError("Gemini generation interrupted by graceful stop")
            now = _utc_now()
            pacing = {}
            if self.pacing_policy is not None:
                pacing = {"pacing_policy": self.pacing_policy, "pacing_epoch": lease.pacing_epoch}
            decision = self.db.record_gemini_generation_start(
                lease.quota_domain_id, lease.model_name,
                key_id=lease.key_id, lease_token=lease.project_lease_token,
                now_ts=_iso_utc(now),
                next_request_at=_iso_utc(now + timedelta(seconds=self._limits.project_model_spacing_seconds)),
                expires_at=_iso_utc(now + timedelta(seconds=self._limits.project_lease_ttl_seconds)),
                **pacing,
            )
            if decision is False:
                raise GeminiRuntimeError("Gemini project lease lost before generation; request cancelled")
            if isinstance(decision, str):
                self._set_wait(run_id, {"mode": "spacing", "wait_until": decision})
                self._sleep_until(min(_parse_ts(decision), now + timedelta(seconds=self._limits.capacity_poll_seconds)))
                continue
            self._set_wait(run_id, {})
            return

    def _report_pacing(self, snapshot: dict, *, run_id: int | None) -> None:
        state = snapshot["state"]
        payload = {
            "scope_id": self.pacing_policy.scope_id,
            "mode": state["mode"], "interval_seconds": snapshot["interval_seconds"],
            "wait_until": state["cooldown_until"] or state["next_start_at"],
            "reason": snapshot["reason"], "probe": snapshot.get("probe", state["mode"] == "probe"),
        }
        self._set_wait(run_id, payload)
        log_message(
            f"gemini pacing: reason={payload['reason']} mode={payload['mode']} "
            f"interval={payload['interval_seconds']}s until={payload['wait_until'] or 'ready'}",
        )

    def _record_pacing_outcome(self, lease: GeminiLease, outcome: PacingOutcome, *, run_id: int | None) -> None:
        if self.pacing_policy is None:
            return
        result = self.db.record_gemini_pacing_outcome(
            self.pacing_policy, quota_domain_id=lease.quota_domain_id,
            lease_token=lease.project_lease_token, epoch=lease.pacing_epoch,
            probe=lease.pacing_probe, outcome=outcome, now_ts=_iso_utc(_utc_now()),
        )
        if result is not None and (result["changed"] or result["probe"]):
            self._report_pacing(result, run_id=run_id)

    @staticmethod
    def _pacing_error_outcome(error: Exception) -> PacingOutcome:
        status = _extract_status_code(error)
        if status == 429:
            return "neutral" if _classify_quota_error(error).daily else "quota"
        if status in {500, 501, 502, 503, 504}:
            return "service"
        if _is_timeout_error(error) or _is_transport_error(error):
            return "transient"
        return "neutral"

    def _lease_heartbeat(self, lease: GeminiLease, stop: threading.Event) -> None:
        while not stop.wait(self._limits.project_lease_heartbeat_seconds):
            now_utc = _utc_now()
            try:
                renewed = self.db.renew_gemini_project_lease(
                    lease.quota_domain_id,
                    lease.project_lease_token,
                    expires_at=_iso_utc(
                        now_utc + timedelta(seconds=self._limits.project_lease_ttl_seconds)
                    ),
                    now_ts=_iso_utc(now_utc),
                )
            except Exception:  # a later heartbeat can recover before lease expiry
                continue
            if not renewed:
                return

    def _handle_error(
        self,
        *,
        lease: GeminiLease,
        error: Exception,
    ) -> None:
        now_utc = _utc_now()
        status_code = _extract_status_code(error)
        error_text = str(error)
        log_message(f"Gemini request failed model={lease.model_name} status={status_code} reason={error_text}", level="WARNING")

        if status_code == 429:
            disposition = _classify_quota_error(error)
            now_ts = _iso_utc(now_utc)
            quota_domain_id = lease.quota_domain_id
            if disposition.daily:
                self.db.mark_gemini_quota_domain_model_exhausted(
                    quota_domain_id, lease.model_name, now_ts=now_ts, error_text=error_text,
                )
                log_message(f"Gemini daily quota exhausted domain={quota_domain_id} model={lease.model_name}")
            else:
                self.db.mark_gemini_error(
                    lease.key_id,
                    lease.model_name,
                    now_ts=now_ts,
                    error_text=error_text,
                    exhausted=False,
                )
                current = self.db.get_gemini_quota_domain_model_state(quota_domain_id, lease.model_name) or {}
                failure_count = int(current.get("failure_count") or 0) + 1
                exponential_delay = min(
                    self._limits.quota_cooldown_max_seconds,
                    self._limits.quota_cooldown_base_seconds * (2 ** min(failure_count - 1, 8)),
                )
                delay_seconds = max(
                    exponential_delay,
                    int(disposition.retry_after_seconds or 0),
                )
                cooldown_until = now_utc + timedelta(seconds=delay_seconds)
                self.db.set_gemini_quota_domain_model_cooldown(
                    quota_domain_id, lease.model_name, cooldown_until=_iso_utc(cooldown_until),
                    failure_count=failure_count, now_ts=now_ts, error_text=error_text,
                )
                log_message(f"Gemini quota cooldown domain={quota_domain_id} model={lease.model_name} until={_iso_utc(cooldown_until)}")
                domain_count = self.db.record_gemini_generic_quota_signal(
                    model_name=lease.model_name, quota_domain_id=quota_domain_id,
                    now_ts=now_ts, window_start_ts=_iso_utc(
                        now_utc - timedelta(seconds=self._limits.generic_429_window_seconds)),
                )
                if (
                    domain_count
                    >= self._limits.generic_429_circuit_breaker_threshold
                ):
                    pause_until = now_utc + timedelta(
                        seconds=self._limits.generic_429_pause_seconds
                    )
                    self.db.set_gemini_model_pause(
                        lease.model_name, _iso_utc(pause_until), reason="generic_429_circuit_breaker",
                    )
                    log_message(f"Gemini model quota circuit paused model={lease.model_name} until={_iso_utc(pause_until)}")
            raise GeminiQuotaExceededError(
                f"Gemini quota unavailable for domain={quota_domain_id} "
                f"model={lease.model_name}"
            ) from error

        if status_code == 400 and _is_terminated_upload_error(error):
            self.db.mark_gemini_error(
                lease.key_id,
                lease.model_name,
                now_ts=_iso_utc(now_utc),
                error_text=error_text,
                exhausted=False,
            )
            raise GeminiTransportError(
                f"Gemini upload session terminated for model={lease.model_name}: "
                f"{error_text}"
            ) from error

        if status_code == 400:
            raise GeminiRequestRejectedError(
                f"Gemini request rejected (400) for model={lease.model_name}: {error_text}"
            ) from error

        if status_code is not None and 500 <= status_code <= 599:
            pause_until = now_utc + timedelta(seconds=self._limits.model_server_pause_seconds)
            self.db.mark_gemini_error(
                lease.key_id,
                lease.model_name,
                now_ts=_iso_utc(now_utc),
                error_text=error_text,
                exhausted=False,
            )
            self.db.set_gemini_model_pause(
                lease.model_name, _iso_utc(pause_until), reason=f"gemini_{status_code}",
            )
            log_message(f"Gemini service pause model={lease.model_name} until={_iso_utc(pause_until)}")
            raise GeminiServerPauseError(
                f"Gemini server error {status_code}; model {lease.model_name} paused until {_iso_utc(pause_until)}",
                model_name=lease.model_name,
                pause_until=pause_until,
            ) from error

        if _is_timeout_error(error):
            self.db.mark_gemini_error(
                lease.key_id,
                lease.model_name,
                now_ts=_iso_utc(now_utc),
                error_text=error_text,
                exhausted=False,
            )
            raise GeminiRequestTimeoutError(
                f"Gemini request timed out for model={lease.model_name}: {error_text}"
            ) from error

        if _is_transport_error(error):
            self.db.mark_gemini_error(
                lease.key_id,
                lease.model_name,
                now_ts=_iso_utc(now_utc),
                error_text=error_text,
                exhausted=False,
            )
            raise GeminiTransportError(
                f"Gemini transport failed for model={lease.model_name}: {error_text}"
            ) from error

        self.db.mark_gemini_error(
            lease.key_id,
            lease.model_name,
            now_ts=_iso_utc(now_utc),
            error_text=error_text,
            exhausted=False,
        )
        raise GeminiRuntimeError(error_text) from error
