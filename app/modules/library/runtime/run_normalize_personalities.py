"""Normalize exact raw bibliographic people into canonical personality records."""

# ruff: noqa: E402
from __future__ import annotations

import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
from pathlib import Path
import signal
import sys
import threading
from typing import Any, Callable, Sequence


def _bootstrap_repo_root() -> None:
    root = Path(__file__).resolve().parents[4]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_bootstrap_repo_root()

from app.db import Database
from app.gemini_config import load_required_gemini_model_pool
from app.gemini_model_pool import (
    GeminiModelPoolExhaustedError,
    GeminiModelPoolItemRejectedError,
    GeminiModelPoolOperationalError,
    GeminiModelPoolUnavailableError,
    GeminiModelResponseError,
    run_ordered_model_pool,
)
from app.gemini_requests import generate_structured_json
from app.gemini_runtime import GeminiRuntimeManager, GeminiStopRequestedError
from app.gemini_workers import current_gemini_worker_id, emit_gemini_worker_log, resolve_gemini_workers
from app.modules.library.personality_normalization import (
    PersonalityResponse,
    PersonalityCandidate,
    build_canonical_name,
    extract_personality_candidates,
    personality_identity_key,
    personality_source_fingerprint,
    preprocess_personality_name_for_model,
    storage_components,
)
from app.modules.library.personality_normalization_prompt import (
    PERSONALITY_NORMALIZATION_PROMPT_VERSION,
    build_personality_normalization_prompt,
)
from app.run_artifact_channel import emit_run_artifact
from app.settings import load_settings


TASK_ID = "library.normalize_personalities"
PANEL_ID = "library"
SCHEMA_VERSION = "person-outcomes-v2"
_LEGACY_SCHEMA_VERSION = "person-components-v1"
_LEGACY_PROMPT_VERSION = "personality-components-v8"
_RESPONSE_LOG_MAX_CHARS = 8_000
_PREPROCESSING_COMPATIBLE_PROMPT_VERSION = "personality-components-v7"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize Library personalities")
    parser.add_argument("--limit", type=int, default=None, help="Optional candidate cap")
    parser.add_argument("--workers", type=int, default=None)
    return parser.parse_args()


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value.isdigit() or int(value) < 1:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


def _checkpoint_attempts(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    value = (checkpoint or {}).get("attempted_models") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    return dict(value) if isinstance(value, dict) else {}


def _checkpoint_attempts_for_candidate(
    checkpoint: dict[str, Any] | None, candidate: PersonalityCandidate,
) -> dict[str, Any]:
    """Release exclusions only for corrected input, explicit retry or fixed rules."""
    attempts = _checkpoint_attempts(checkpoint)
    source_changed = bool(checkpoint and checkpoint.get("source_fingerprint")
                          and checkpoint["source_fingerprint"] != personality_source_fingerprint(candidate))
    decision_rules_changed = bool(checkpoint and checkpoint.get("state") in {"not_person", "unusable"}
                                  and (checkpoint.get("prompt_version") != PERSONALITY_NORMALIZATION_PROMPT_VERSION
                                       or checkpoint.get("schema_version") != SCHEMA_VERSION))
    explicit_retry = (checkpoint or {}).get("state") == "retry_requested"
    for model, failure in list(attempts.items()):
        if (isinstance(failure, dict) and failure.get("kind") in {"response", "timeout", "recovery_blocked"}
                and (source_changed or decision_rules_changed or explicit_retry)):
            attempts[model] = {**failure, "kind": "recovered_response"}
            continue
        if not isinstance(failure, dict) or failure.get("kind") != "response":
            continue
        error = str(failure.get("error") or "")
        legacy_empty = (
            (checkpoint or {}).get("schema_version") == _LEGACY_SCHEMA_VERSION
            and "at least one usable surname or personal-name component is required" in error
        )
        evidence = candidate.raw_name + " " + error
        initials_fixed = "initial fields must contain one initial such as" in error and re.search(r"(?<![\w.])(?:Kh|Sh|Ts|Ju)\.(?![\w.])", evidence, re.IGNORECASE)
        if legacy_empty or initials_fixed:
            attempts[model] = {**failure, "kind": "recovered_response"}
    return attempts


def _excluded_models(attempts: dict[str, Any]) -> set[str]:
    return {model for model, failure in attempts.items()
            if not isinstance(failure, dict) or failure.get("kind") not in {"recovered_response", "decision", "transient"}}


def format_personality_response_for_log(response: Any) -> str:
    """Render one bounded model response for the worker-attributed task log."""
    raw = str(response or "")
    if len(raw) > _RESPONSE_LOG_MAX_CHARS:
        return json.dumps(
            {
                "response_truncated": True,
                "invalid_response": raw[:_RESPONSE_LOG_MAX_CHARS],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = {"invalid_response": raw}
    return json.dumps(decoded, ensure_ascii=False, indent=2, sort_keys=True)


def _eligible_candidates(
    candidates: Sequence[PersonalityCandidate], checkpoints: Sequence[dict[str, Any]]
) -> tuple[list[PersonalityCandidate], int]:
    by_name = {str(item.get("raw_name") or ""): item for item in checkpoints}
    eligible: list[PersonalityCandidate] = []
    skipped = 0
    for candidate in candidates:
        checkpoint = by_name.get(candidate.raw_name)
        model_input_unchanged = (
            preprocess_personality_name_for_model(candidate.raw_name) == candidate.raw_name
        )
        prompt_current = checkpoint and (
            checkpoint.get("prompt_version") in {PERSONALITY_NORMALIZATION_PROMPT_VERSION, _LEGACY_PROMPT_VERSION}
            or (
                model_input_unchanged
                and checkpoint.get("prompt_version") == _PREPROCESSING_COMPATIBLE_PROMPT_VERSION
            )
        )
        current = (
            checkpoint
            and checkpoint.get("source_fingerprint") == personality_source_fingerprint(candidate)
            and prompt_current
            and checkpoint.get("schema_version") in {SCHEMA_VERSION, _LEGACY_SCHEMA_VERSION}
        )
        if current and checkpoint.get("state") == "succeeded":
            skipped += 1
            continue
        if (checkpoint and checkpoint.get("state") in {"not_person", "unusable"}
                and checkpoint.get("source_fingerprint") == personality_source_fingerprint(candidate)
                and checkpoint.get("prompt_version") == PERSONALITY_NORMALIZATION_PROMPT_VERSION
                and checkpoint.get("schema_version") == SCHEMA_VERSION):
            skipped += 1
            continue
        recovered = any(failure.get("kind") == "recovered_response"
                        for failure in _checkpoint_attempts_for_candidate(checkpoint, candidate).values()
                        if isinstance(failure, dict))
        if current and checkpoint.get("state") == "failed" and not checkpoint.get("retryable") and not recovered:
            skipped += 1
            continue
        eligible.append(candidate)
    # Stable partition before applying any candidate limit. Durable checkpoints
    # retain this priority across restarts, including interrupted processing.
    eligible.sort(key=lambda candidate: candidate.raw_name in by_name)
    return eligible, skipped


class _WorkQueue:
    """One shared first pass followed by at most one retry per unique person."""

    def __init__(self, candidates: Sequence[PersonalityCandidate], should_stop: Callable[[], bool]):
        self.initial = deque(candidates)
        self.tail: deque[PersonalityCandidate] = deque()
        self.condition = threading.Condition()
        self.should_stop = should_stop
        self.active = 0
        self.retry_phase = False
        self.turns: Counter[str] = Counter()
        self.states: dict[str, str] = {}
        self.model_attempts: Counter[str] = Counter()
        self.model_successes: Counter[str] = Counter()
        self.outcome = "completed"

    def claim(self) -> PersonalityCandidate | None:
        with self.condition:
            while not self.should_stop() and self.outcome == "completed":
                if self.initial:
                    candidate = self.initial.popleft()
                elif self.retry_phase and self.tail:
                    candidate = self.tail.popleft()
                elif self.active:
                    self.condition.wait(timeout=0.1)
                    continue
                elif self.tail:
                    self.retry_phase = True
                    continue
                else:
                    return None
                self.active += 1
                self.turns[candidate.raw_name] += 1
                return candidate
            if self.should_stop():
                self.outcome = "stopped"
            return None

    def finish(self, candidate: PersonalityCandidate, state: str, *, retry: bool = False) -> bool:
        with self.condition:
            requeued = retry and self.turns[candidate.raw_name] == 1
            if requeued:
                self.tail.append(candidate)
                state = "retry_pending"
            self.states[candidate.raw_name] = state
            self.active -= 1
            self.condition.notify_all()
            return requeued

    def snapshot(self, total: int) -> dict[str, Any]:
        with self.condition:
            counts = Counter(self.states.values())
            final_states = ("succeeded", "not_person", "unusable", "deferred", "failed")
            processed = sum(counts[state] for state in final_states)
            return {"current": processed, "total": total, "processed": processed,
                    **{state: counts[state] for state in final_states}, "skipped": 0,
                    "retry_pending": counts["retry_pending"],
                    "model_attempts": dict(self.model_attempts),
                    "model_successes": dict(self.model_successes)}


def run_personality_normalization(
    *, db: Any, models: Sequence[str], run_id: int | None,
    should_stop: Callable[[], bool],
    request_json: Callable[..., str] = generate_structured_json,
    candidates: Sequence[PersonalityCandidate] | None = None,
    limit: int | None = None, workers: int = 1,
) -> dict[str, Any]:
    """Persist explicit decisions and process one bounded shared retry queue."""
    source = list(candidates) if candidates is not None else extract_personality_candidates(db.list_personality_source_documents())
    # One queue entry per raw name even when a caller supplies duplicate candidates.
    source = list({candidate.raw_name: candidate for candidate in source}.values())
    eligible, skipped = _eligible_candidates(source, db.list_personality_checkpoints())
    if limit is not None:
        eligible = eligible[:max(0, int(limit))]
    queue = _WorkQueue(eligible, should_stop)
    progress_lock = threading.Lock()

    def publish(*, force: bool = False) -> None:
        if run_id is not None:
            with progress_lock:
                db.publish_run_progress(task_id=TASK_ID, run_id=run_id, panel_id=PANEL_ID,
                                        progress=queue.snapshot(len(eligible)), force=force)

    def process(candidate: PersonalityCandidate, manager: GeminiRuntimeManager, worker_id: str) -> None:
        checkpoint = db.get_personality_checkpoint(candidate.raw_name)
        attempts = _checkpoint_attempts_for_candidate(checkpoint, candidate)
        fingerprint = personality_source_fingerprint(candidate)
        model_input = preprocess_personality_name_for_model(candidate.raw_name)
        base = {"raw_name": candidate.raw_name, "source_fingerprint": fingerprint,
                "document_count": candidate.document_count, "mention_count": candidate.mention_count,
                "source_roles": list(candidate.roles), "prompt_version": PERSONALITY_NORMALIZATION_PROMPT_VERSION,
                "schema_version": SCHEMA_VERSION}
        emit_gemini_worker_log(f"library personalities: person start raw_name={candidate.raw_name}", worker_id=worker_id)

        def call_model(model_name: str, api_key: str, _lease: Any) -> str:
            with queue.condition:
                queue.model_attempts[model_name] += 1
            emit_gemini_worker_log(f"library personalities: model attempt raw_name={candidate.raw_name} model={model_name}", worker_id=worker_id)
            raw = request_json(api_key=api_key, model_name=model_name,
                               contents=[build_personality_normalization_prompt(model_input, document_languages=candidate.document_languages)],
                               response_schema=PersonalityResponse)
            emit_gemini_worker_log(f"library personalities: model response raw_name={candidate.raw_name} model={model_name}\n"
                                  + format_personality_response_for_log(raw), worker_id=worker_id)
            return raw

        def parse(raw: str) -> PersonalityResponse:
            try:
                return PersonalityResponse.model_validate_json(raw)
            except ValueError as exc:
                raise GeminiModelResponseError(str(exc)) from exc

        def record_failure(model_name: str, kind: str, error: str) -> None:
            previous = attempts.get(model_name)
            attempts[model_name] = {"kind": kind, "error": error}
            if isinstance(previous, dict) and previous.get("kind") in {"recovered_response", "transient"}:
                attempts[model_name]["previous_failure"] = previous
            db.save_personality_checkpoint(**base, state="processing", attempted_models=attempts,
                                           failure_context=error, retryable=False)

        state = "failed"
        retry = False
        wait_until = None
        try:
            result = run_ordered_model_pool(manager=manager, models=models, request=call_model,
                parse=parse, record_failure=record_failure, run_id=run_id,
                already_attempted=_excluded_models(attempts), yield_on_transient=True)
        except (GeminiModelPoolExhaustedError, GeminiModelPoolItemRejectedError) as exc:
            if isinstance(exc, GeminiModelPoolItemRejectedError):
                attempts = {model: {**failure, "kind": "recovery_blocked", "recovery_blocked_by": str(exc)}
                            if isinstance(failure, dict) and failure.get("kind") == "recovered_response" else failure
                            for model, failure in attempts.items()}
            db.save_personality_checkpoint(**base, state=state, attempted_models=attempts,
                                           failure_context=str(exc), retryable=False)
        except GeminiModelPoolUnavailableError as exc:
            state = "deferred"
            db.save_personality_checkpoint(**base, state=state, attempted_models=attempts,
                                           failure_context=str(exc), retryable=True)
            retry = exc.retry_at is not None
            wait_until = exc.retry_at
            if not retry:
                # No usable pool or known retry time: preserve untouched work.
                with queue.condition:
                    queue.outcome = "deferred"
                    queue.condition.notify_all()
        except GeminiModelPoolOperationalError as exc:
            state = "deferred" if exc.retryable else "failed"
            retry = exc.retryable
            wait_until = exc.retry_at
            if exc.model_name:
                previous = attempts.get(exc.model_name)
                attempts[exc.model_name] = {"kind": "transient", "error": str(exc)}
                if previous:
                    attempts[exc.model_name]["previous_failure"] = previous
            db.save_personality_checkpoint(**base, state=state, attempted_models=attempts,
                                           failure_context=str(exc), retryable=retry)
            emit_gemini_worker_log(f"library personalities: person {state} raw_name={candidate.raw_name} reason={exc}", worker_id=worker_id)
        except GeminiStopRequestedError:
            with queue.condition:
                queue.outcome = "stopped"
            # An existing deferred checkpoint stays durable across stop during retry.
            state = "deferred" if checkpoint and checkpoint.get("retryable") else "pending"
        else:
            decision = result.value
            components = decision.person_components()
            state = "succeeded" if components is not None else decision.outcome
            if components is None:
                attempts[result.model_name] = {"kind": "decision", "outcome": decision.outcome,
                    "reason": decision.reason, "document_languages": list(candidate.document_languages)}
                db.save_personality_checkpoint(**base, state=state, attempted_models=attempts,
                                               failure_context=decision.reason, retryable=False, completed=True)
                emit_gemini_worker_log(f"library personalities: person decision raw_name={candidate.raw_name} outcome={state} reason={decision.reason}", worker_id=worker_id)
            else:
                canonical = db.persist_personality_normalization(**base, components=storage_components(components),
                    display_name=build_canonical_name(components), identity_key=personality_identity_key(components), model=result.model_name)
                with queue.condition:
                    queue.model_successes[result.model_name] += 1
                emit_gemini_worker_log(f"library personalities: person success raw_name={candidate.raw_name} canonical_id={canonical['canonical_id']}", worker_id=worker_id)
        if retry and queue.turns[candidate.raw_name] == 1:
            emit_gemini_worker_log(f"library personalities: queue tail raw_name={candidate.raw_name} retry=1/1", worker_id=worker_id)
        if wait_until is not None:
            # Keep this turn active until the pause ends so another worker cannot
            # consume its only retry while the model is still paused.
            emit_gemini_worker_log(f"library personalities: waiting until={wait_until.isoformat()} before next person", worker_id=worker_id)
            try:
                manager._sleep_until(wait_until)
            except GeminiStopRequestedError:
                with queue.condition:
                    queue.outcome = "stopped"
                    queue.condition.notify_all()
        queue.finish(candidate, state, retry=retry)
        publish()

    def work() -> None:
        worker_id = current_gemini_worker_id("personalities")
        manager = GeminiRuntimeManager(db, task_id=TASK_ID, panel_id=PANEL_ID, should_stop=should_stop, worker_id=worker_id)
        while (candidate := queue.claim()) is not None:
            try:
                process(candidate, manager, worker_id)
            except BaseException:
                with queue.condition:
                    queue.outcome = "failed"
                    queue.active -= 1
                    queue.condition.notify_all()
                raise

    publish()
    emit_gemini_worker_log(f"library personalities: start eligible={len(eligible)} total={len(source)}", worker_id="coordinator")
    count = min(max(1, int(workers)), max(1, len(eligible)))
    if count == 1:
        work()
    else:
        with ThreadPoolExecutor(max_workers=count, thread_name_prefix="personalities-worker") as executor:
            list(executor.map(lambda _index: work(), range(count)))
    with queue.condition:
        queue.states = {name: "deferred" if state == "retry_pending" else state for name, state in queue.states.items()}
    publish(force=True)
    snapshot = queue.snapshot(len(eligible))
    return {"kind": "library.personality_normalization_summary", **snapshot,
            "outcome": queue.outcome, "total": len(source), "processed": snapshot["processed"] + skipped,
            "skipped": skipped, "workers": count, "remaining": len(eligible) - snapshot["processed"]}


def main() -> None:
    args = _parse_args()
    workers = resolve_gemini_workers(args.workers)
    settings = load_settings()
    db = Database(settings.database_url, schema=settings.database_schema, local_state_path=settings.local_state_path)
    stop = {"requested": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("requested", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("requested", True))
    emit_gemini_worker_log(f"library personalities: configured workers={workers}", worker_id="coordinator")
    summary = run_personality_normalization(db=db, models=load_required_gemini_model_pool(), run_id=_run_id(), should_stop=lambda: stop["requested"], limit=args.limit, workers=workers)
    emit_run_artifact(summary)
    emit_gemini_worker_log(f"library personalities: final {json.dumps(summary, ensure_ascii=False, sort_keys=True)}", worker_id="coordinator")


if __name__ == "__main__":
    main()
