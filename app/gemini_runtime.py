"""Unified Gemini key/runtime coordination for all Manzara tasks."""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.db import Database
from app.gemini_config import GeminiKey, load_gemini_keys


_PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
_UTC = timezone.utc
_KEY_COOLDOWN_SECONDS = 60
_GLOBAL_SERVER_PAUSE_SECONDS = 60
_MAX_WAIT_SLICE_SECONDS = 10


class GeminiRuntimeError(RuntimeError):
    """Base runtime error for Gemini coordination."""


class GeminiQuotaExceededError(GeminiRuntimeError):
    """Raised when Gemini returns 429 for a key+model."""


class GeminiAllKeysExhaustedError(GeminiRuntimeError):
    """Raised when no non-exhausted keys remain for model."""


class GeminiServerPauseError(GeminiRuntimeError):
    """Raised when Gemini returns 5xx and global pause has been activated."""


class GeminiRequestRejectedError(GeminiRuntimeError):
    """Raised when Gemini rejects one request payload (e.g. HTTP 400)."""


class GeminiRequestTimeoutError(GeminiRuntimeError):
    """Raised when one model request exceeds its response deadline."""


class GeminiStopRequestedError(GeminiRuntimeError):
    """Raised when a task requests graceful stop while waiting for Gemini."""


@dataclass(frozen=True)
class GeminiLease:
    """Reserved key context for one Gemini request attempt."""

    account_id: str
    key_id: str
    key_value: str
    masked_key: str
    model_name: str


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


class GeminiRuntimeManager:
    """Shared Gemini key allocator with DB-backed runtime state + SSE events."""

    def __init__(
        self,
        db: Database,
        *,
        task_id: Optional[str],
        panel_id: Optional[str],
        should_stop: Optional[Callable[[], bool]] = None,
    ):
        self.db = db
        self.task_id = task_id
        self.panel_id = panel_id
        self.should_stop = should_stop or (lambda: False)
        self._rand = random.SystemRandom()
        self._local_lock = threading.Lock()

    def _emit(
        self, event_type: str, payload: Dict[str, Any], *, run_id: Optional[int] = None
    ) -> None:
        self.db.insert_event(
            event_type,
            task_id=self.task_id,
            run_id=run_id,
            panel_id=self.panel_id,
            payload=payload,
        )

    def _sync_key_registry(self) -> List[GeminiKey]:
        with self._local_lock:
            keys = load_gemini_keys()
            self.db.upsert_gemini_keys(
                [
                    {
                        "key_id": item.key_id,
                        "account_id": item.account_id,
                        "masked_key": item.masked_key,
                    }
                    for item in keys
                ]
            )
            return keys

    @staticmethod
    def _cycle_label(now_utc: datetime) -> str:
        return now_utc.astimezone(_PACIFIC_TZ).date().isoformat()

    @staticmethod
    def _blackout_window(now_utc: datetime) -> Dict[str, Any]:
        pt_now = now_utc.astimezone(_PACIFIC_TZ)
        midnight_today = datetime(
            pt_now.year, pt_now.month, pt_now.day, tzinfo=_PACIFIC_TZ
        )
        midnight_next = midnight_today + timedelta(days=1)

        prev_start = midnight_today - timedelta(hours=1)
        prev_end = midnight_today + timedelta(hours=1)
        next_start = midnight_next - timedelta(hours=1)
        next_end = midnight_next + timedelta(hours=1)

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

    def _ensure_cycle(self, now_utc: datetime) -> Dict[str, Any]:
        cycle_label = self._cycle_label(now_utc)
        control = self.db.ensure_gemini_runtime_control(cycle_label)
        rolled = self.db.rollover_gemini_cycle(cycle_label)
        if rolled:
            self._emit(
                "gemini.all_reset",
                {
                    "cycle_label": cycle_label,
                    "reason": "daily_reset",
                },
            )
            control = self.db.ensure_gemini_runtime_control(cycle_label)
        return control

    def _clear_elapsed_pause_if_needed(
        self, control: Dict[str, Any], now_utc: datetime
    ) -> Dict[str, Any]:
        pause_until = _parse_ts(control.get("pause_until"))
        if pause_until is None or pause_until > now_utc:
            return control
        updated = self.db.set_gemini_pause(None, None)
        self._emit(
            "gemini.pause.ended",
            {
                "ended_at": _iso_utc(now_utc),
            },
        )
        return updated

    def _wait_reason(
        self, control: Dict[str, Any], now_utc: datetime
    ) -> Optional[Dict[str, Any]]:
        blackout = self._blackout_window(now_utc)
        if blackout["active"]:
            return {
                "type": "blackout",
                "wait_until": _parse_ts(blackout.get("wait_until_utc")),
                "blackout": blackout,
            }
        pause_until = _parse_ts(control.get("pause_until"))
        if pause_until is not None and pause_until > now_utc:
            return {
                "type": "pause",
                "wait_until": pause_until,
                "pause_reason": str(control.get("last_pause_reason") or ""),
            }
        return None

    def _ensure_model_rows(self, keys: List[GeminiKey], model_name: str) -> None:
        for key in keys:
            self.db.ensure_gemini_model_state(key.key_id, model_name)

    def _pick_candidate(
        self,
        *,
        keys: List[GeminiKey],
        model_name: str,
        now_utc: datetime,
    ) -> Dict[str, Any]:
        state_rows = self.db.list_gemini_model_states(model_name=model_name)
        by_key_id: Dict[str, Dict[str, Any]] = {}
        for row in state_rows:
            key_id = str(row.get("key_id") or "")
            row_model = str(row.get("model_name") or "")
            if not key_id or row_model != model_name:
                continue
            by_key_id[key_id] = row

        available_by_account: Dict[str, List[GeminiKey]] = {}
        ready_by_account: Dict[str, List[GeminiKey]] = {}
        earliest_cooldown: Optional[datetime] = None

        for key in keys:
            row = by_key_id.get(key.key_id) or {}
            if bool(row.get("exhausted", False)):
                continue
            available_by_account.setdefault(key.account_id, []).append(key)
            cooldown_until = _parse_ts(row.get("cooldown_until"))
            if cooldown_until is None or cooldown_until <= now_utc:
                ready_by_account.setdefault(key.account_id, []).append(key)
            else:
                if earliest_cooldown is None or cooldown_until < earliest_cooldown:
                    earliest_cooldown = cooldown_until

        if not available_by_account:
            raise GeminiAllKeysExhaustedError(
                f"All Gemini keys exhausted for model '{model_name}'"
            )

        if not ready_by_account:
            return {
                "type": "wait",
                "wait_until": earliest_cooldown,
            }

        account_choices = sorted(ready_by_account.keys())
        account_id = self._rand.choice(account_choices)
        key = self._rand.choice(ready_by_account[account_id])
        return {
            "type": "key",
            "key": key,
        }

    def _sleep_until(self, wait_until: Optional[datetime]) -> None:
        if wait_until is None:
            if self.should_stop():
                raise GeminiStopRequestedError(
                    "Gemini wait interrupted by graceful stop"
                )
            time.sleep(1.0)
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
            time.sleep(min(float(remaining), _MAX_WAIT_SLICE_SECONDS))

    def acquire_key(
        self, *, model_name: str, run_id: Optional[int] = None
    ) -> GeminiLease:
        """Block until one key+model slot is available and reserve one-minute cooldown."""
        while True:
            if self.should_stop():
                raise GeminiStopRequestedError(
                    "Gemini request skipped after graceful stop"
                )
            now_utc = _utc_now()
            keys = self._sync_key_registry()
            if not keys:
                raise GeminiAllKeysExhaustedError("No Gemini keys configured")

            control = self._ensure_cycle(now_utc)
            control = self._clear_elapsed_pause_if_needed(control, now_utc)

            wait_gate = self._wait_reason(control, now_utc)
            if wait_gate is not None:
                self._sleep_until(wait_gate.get("wait_until"))
                continue

            self._ensure_model_rows(keys, model_name)
            decision = self._pick_candidate(
                keys=keys, model_name=model_name, now_utc=now_utc
            )
            if decision.get("type") == "wait":
                self._sleep_until(decision.get("wait_until"))
                continue

            key = decision["key"]
            cooldown_until = now_utc + timedelta(seconds=_KEY_COOLDOWN_SECONDS)
            claimed = self.db.try_claim_gemini_key_use(
                key.key_id,
                model_name,
                now_ts=_iso_utc(now_utc),
                cooldown_until=_iso_utc(cooldown_until),
            )
            if not claimed:
                continue

            self._emit(
                "gemini.key.used",
                {
                    "account_id": key.account_id,
                    "key_id": key.key_id,
                    "masked_key": key.masked_key,
                    "model_name": model_name,
                    "cooldown_until": _iso_utc(cooldown_until),
                },
                run_id=run_id,
            )
            return GeminiLease(
                account_id=key.account_id,
                key_id=key.key_id,
                key_value=key.key_value,
                masked_key=key.masked_key,
                model_name=model_name,
            )

    def _handle_error(
        self,
        *,
        lease: GeminiLease,
        error: Exception,
        run_id: Optional[int],
    ) -> None:
        now_utc = _utc_now()
        status_code = _extract_status_code(error)
        error_text = str(error)

        if status_code == 429:
            self.db.mark_gemini_error(
                lease.key_id,
                lease.model_name,
                now_ts=_iso_utc(now_utc),
                error_text=error_text,
                exhausted=True,
            )
            self._emit(
                "gemini.key.exhausted",
                {
                    "account_id": lease.account_id,
                    "key_id": lease.key_id,
                    "masked_key": lease.masked_key,
                    "model_name": lease.model_name,
                    "status_code": status_code,
                    "error": error_text,
                },
                run_id=run_id,
            )
            raise GeminiQuotaExceededError(
                f"Gemini quota exhausted for key {lease.masked_key} model={lease.model_name}"
            ) from error

        if status_code == 400:
            self._emit(
                "gemini.request.rejected",
                {
                    "account_id": lease.account_id,
                    "key_id": lease.key_id,
                    "masked_key": lease.masked_key,
                    "model_name": lease.model_name,
                    "status_code": status_code,
                    "error": error_text,
                },
                run_id=run_id,
            )
            raise GeminiRequestRejectedError(
                f"Gemini request rejected (400) for model={lease.model_name}: {error_text}"
            ) from error

        if status_code is not None and 500 <= status_code <= 599:
            pause_until = now_utc + timedelta(seconds=_GLOBAL_SERVER_PAUSE_SECONDS)
            self.db.mark_gemini_error(
                lease.key_id,
                lease.model_name,
                now_ts=_iso_utc(now_utc),
                error_text=error_text,
                exhausted=False,
            )
            self.db.set_gemini_pause(
                _iso_utc(pause_until), reason=f"gemini_{status_code}"
            )
            self._emit(
                "gemini.pause.started",
                {
                    "pause_until": _iso_utc(pause_until),
                    "status_code": status_code,
                    "reason": error_text,
                },
                run_id=run_id,
            )
            raise GeminiServerPauseError(
                f"Gemini server error {status_code}; paused until {_iso_utc(pause_until)}"
            ) from error

        if _is_timeout_error(error):
            self.db.mark_gemini_error(
                lease.key_id,
                lease.model_name,
                now_ts=_iso_utc(now_utc),
                error_text=error_text,
                exhausted=False,
            )
            self._emit(
                "gemini.request.timeout",
                {
                    "account_id": lease.account_id,
                    "key_id": lease.key_id,
                    "masked_key": lease.masked_key,
                    "model_name": lease.model_name,
                    "error": error_text,
                },
                run_id=run_id,
            )
            raise GeminiRequestTimeoutError(
                f"Gemini request timed out for model={lease.model_name}: {error_text}"
            ) from error

        self.db.mark_gemini_error(
            lease.key_id,
            lease.model_name,
            now_ts=_iso_utc(now_utc),
            error_text=error_text,
            exhausted=False,
        )
        self._emit(
            "gemini.key.error",
            {
                "account_id": lease.account_id,
                "key_id": lease.key_id,
                "masked_key": lease.masked_key,
                "model_name": lease.model_name,
                "status_code": status_code,
                "error": error_text,
            },
            run_id=run_id,
        )
        raise GeminiRuntimeError(error_text) from error

    def run_with_key(
        self,
        *,
        model_name: str,
        call: Callable[[str, GeminiLease], Any],
        run_id: Optional[int] = None,
        max_attempts: int = 2,
    ) -> Any:
        """Execute Gemini call with key selection/pause/exhaustion policy."""
        attempts = max(1, int(max_attempts))
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            lease = self.acquire_key(model_name=model_name, run_id=run_id)
            try:
                result = call(lease.key_value, lease)
            except Exception as error:  # noqa: BLE001
                try:
                    self._handle_error(lease=lease, error=error, run_id=run_id)
                except GeminiRequestRejectedError:
                    raise
                except GeminiServerPauseError as pause_error:
                    last_error = pause_error
                    if attempt < attempts:
                        continue
                    raise
                except GeminiQuotaExceededError:
                    raise
                except GeminiRuntimeError as runtime_error:
                    last_error = runtime_error
                    if attempt < attempts:
                        continue
                    raise
            else:
                now_utc = _utc_now()
                self.db.mark_gemini_success(
                    lease.key_id,
                    lease.model_name,
                    now_ts=_iso_utc(now_utc),
                )
                self._emit(
                    "gemini.key.success",
                    {
                        "account_id": lease.account_id,
                        "key_id": lease.key_id,
                        "masked_key": lease.masked_key,
                        "model_name": lease.model_name,
                        "last_success_at": _iso_utc(now_utc),
                    },
                    run_id=run_id,
                )
                return result

        if last_error is not None:
            raise last_error
        raise GeminiRuntimeError("Gemini call failed without explicit error")

    def reset_key(self, key_id: str, *, run_id: Optional[int] = None) -> int:
        """Clear exhausted flags for one key across all models."""
        changed = self.db.reset_gemini_key_exhaustion(key_id)
        self._emit(
            "gemini.key.reset",
            {
                "key_id": key_id,
                "rows_changed": changed,
            },
            run_id=run_id,
        )
        return changed

    def reset_all(self, *, run_id: Optional[int] = None) -> int:
        """Clear exhausted flags for all keys and models."""
        changed = self.db.reset_all_gemini_exhaustion()
        self._emit(
            "gemini.all_reset",
            {
                "reason": "manual_reset",
                "rows_changed": changed,
            },
            run_id=run_id,
        )
        return changed

    def snapshot(self) -> Dict[str, Any]:
        """Return current Gemini runtime status grouped by account and key."""
        now_utc = _utc_now()
        keys = self._sync_key_registry()
        control = self._ensure_cycle(now_utc)
        control = self._clear_elapsed_pause_if_needed(control, now_utc)
        blackout = self._blackout_window(now_utc)
        state_rows = self.db.list_gemini_model_states(model_name=None)

        models_by_key: Dict[str, List[Dict[str, Any]]] = {}
        for row in state_rows:
            key_id = str(row.get("key_id") or "")
            model_name = str(row.get("model_name") or "").strip()
            if not key_id or not model_name:
                continue
            models_by_key.setdefault(key_id, []).append(
                {
                    "model_name": model_name,
                    "exhausted": bool(row.get("exhausted", False)),
                    "exhausted_at": row.get("exhausted_at"),
                    "cooldown_until": row.get("cooldown_until"),
                    "last_used_at": row.get("last_used_at"),
                    "last_success_at": row.get("last_success_at"),
                    "last_error_at": row.get("last_error_at"),
                    "last_error_text": row.get("last_error_text"),
                    "attempts_total": int(row.get("attempts_total") or 0),
                    "attempts_cycle": int(row.get("attempts_cycle") or 0),
                    "success_total": int(row.get("success_total") or 0),
                    "success_cycle": int(row.get("success_cycle") or 0),
                }
            )

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for key in keys:
            model_rows = sorted(
                models_by_key.get(key.key_id, []),
                key=lambda item: str(item.get("model_name") or ""),
            )
            exhausted_models = [
                item["model_name"] for item in model_rows if item.get("exhausted")
            ]
            grouped.setdefault(key.account_id, []).append(
                {
                    "key_id": key.key_id,
                    "masked_key": key.masked_key,
                    "models": model_rows,
                    "exhausted_models": exhausted_models,
                }
            )

        accounts = [
            {
                "account_id": account_id,
                "key_count": len(items),
                "keys": sorted(items, key=lambda item: str(item.get("key_id") or "")),
            }
            for account_id, items in sorted(grouped.items(), key=lambda item: item[0])
        ]

        exhausted_rows = 0
        known_models = set()
        for account in accounts:
            for key in account["keys"]:
                for model in key["models"]:
                    known_models.add(str(model.get("model_name") or ""))
                    if model.get("exhausted"):
                        exhausted_rows += 1

        pause_until = control.get("pause_until")
        pause_until_dt = _parse_ts(pause_until)
        pause_active = pause_until_dt is not None and pause_until_dt > now_utc

        return {
            "global": {
                "now_utc": _iso_utc(now_utc),
                "timezone_reset": "America/Los_Angeles",
                "cycle_label": str(
                    control.get("cycle_label") or self._cycle_label(now_utc)
                ),
                "pause_until": pause_until,
                "pause_active": bool(pause_active),
                "pause_reason": control.get("last_pause_reason"),
                "blackout_active": bool(blackout["active"]),
                "blackout_start_utc": blackout["start_utc"],
                "blackout_end_utc": blackout["end_utc"],
                "reset_at_utc": blackout["reset_utc"],
            },
            "summary": {
                "accounts": len(accounts),
                "keys": sum(int(account["key_count"]) for account in accounts),
                "models_seen": len([item for item in known_models if item]),
                "exhausted_rows": exhausted_rows,
            },
            "accounts": accounts,
        }
