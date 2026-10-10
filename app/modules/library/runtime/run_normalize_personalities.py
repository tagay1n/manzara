"""Normalize exact raw bibliographic people into canonical personality records."""

from __future__ import annotations

import json
import uuid
from collections import Counter, deque
from typing import Any, Callable, Sequence

from app.catalog.contracts import CatalogConflict
from app.gemini_config import load_required_gemini_model_pool
from app.gemini_model_pool import (
    GeminiModelPoolExhaustedError,
    GeminiModelPoolItemRejectedError,
    GeminiModelPoolOperationalError,
    GeminiModelPoolUnavailableError,
    GeminiModelResponseError,
    run_ordered_model_pool,
)
from app.gemini_pacing import GeminiPacingPolicy
from app.gemini_requests import generate_structured_json
from app.gemini_runtime import GeminiRuntimeManager, GeminiStopRequestedError
from app.modules.library.personality_normalization import (
    PersonalityCandidate,
    PersonalityResponse,
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
from app.runtime_config import config_integer
from app.task_runtime.contracts import RunContext, RunOptions
from app.task_runtime.logging import log_message

TASK_ID = "library.normalize_personalities"
SCHEMA_VERSION = "person-outcomes-v2"


def _checkpoint_attempts(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    value = (checkpoint or {}).get("attempted_models") or {}
    return dict(value)


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
    return attempts


def _excluded_models(attempts: dict[str, Any]) -> set[str]:
    return {model for model, failure in attempts.items()
            if not isinstance(failure, dict) or failure.get("kind") not in {"recovered_response", "decision", "transient"}}


def format_personality_response_for_log(response: Any) -> str:
    """Render one bounded model response for the task log."""
    raw = str(response or "")
    if len(raw) > config_integer("gemini", "request", "response_log_max_chars"):
        return json.dumps(
            {
                "response_truncated": True,
                "invalid_response": raw[:config_integer("gemini", "request", "response_log_max_chars")],
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
        current = (
            checkpoint
            and checkpoint.get("source_fingerprint") == personality_source_fingerprint(candidate)
            and checkpoint.get("prompt_version") == PERSONALITY_NORMALIZATION_PROMPT_VERSION
            and checkpoint.get("schema_version") == SCHEMA_VERSION
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
    """Sequential first pass followed by at most one retry per name."""

    def __init__(self, candidates, should_stop):
        self.initial = deque(candidates)
        self.tail = deque()
        self.should_stop = should_stop
        self.active = 0
        self.turns = Counter()
        self.states = {}
        self.model_attempts = Counter()
        self.model_successes = Counter()
        self.outcome = "completed"

    def claim(self):
        if self.should_stop():
            self.outcome = "stopped"
        if self.outcome != "completed":
            return None
        pending = self.initial if self.initial else self.tail
        if not pending:
            return None
        candidate = pending.popleft()
        self.active = 1
        self.turns[candidate.raw_name] += 1
        return candidate

    def finish(self, candidate, state, *, retry=False):
        if retry and self.turns[candidate.raw_name] == 1:
            self.tail.append(candidate)
            state = "retry_pending"
        self.states[candidate.raw_name] = state
        self.active = 0

    def snapshot(self, total):
        counts = Counter(self.states.values())
        final_states = ("succeeded", "not_person", "unusable", "deferred", "failed")
        processed = sum(counts[state] for state in final_states)
        return {"current": processed, "total": total, "processed": processed,
                "resolved": processed - counts["deferred"], "active": self.active,
                "phase": "processing", "outcome": self.outcome,
                **{state: counts[state] for state in final_states}, "skipped": 0,
                "retry_pending": counts["retry_pending"],
                "model_attempts": dict(self.model_attempts),
                "model_successes": dict(self.model_successes)}


def run_personality_normalization(
    *, db: Any, models: Sequence[str], run_id: int | None,
    should_stop: Callable[[], bool],
    request_json: Callable[..., str] = generate_structured_json,
    candidates: Sequence[PersonalityCandidate] | None = None,
    limit: int | None = None,
    progress_sink: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Persist explicit decisions and process one bounded shared retry queue."""
    RunOptions(limit=limit)
    source = list(candidates) if candidates is not None else extract_personality_candidates(db.list_personality_source_documents())
    # One queue entry per raw name even when a caller supplies duplicate candidates.
    source = list({candidate.raw_name: candidate for candidate in source}.values())
    eligible, skipped = _eligible_candidates(source, db.list_personality_checkpoints())
    if limit is not None:
        eligible = eligible[:limit]
    queue = _WorkQueue(eligible, should_stop)
    # Each new run starts with fresh pacing.
    pacing_policy = GeminiPacingPolicy.from_config(f"{TASK_ID}:{run_id if run_id is not None else uuid.uuid4().hex}")

    def publish(*, force: bool = False) -> None:
        if run_id is not None or progress_sink is not None:
            snapshot = {**queue.snapshot(len(eligible)), "skipped": skipped, "source_total": len(source)}
            if progress_sink is not None:
                progress_sink(snapshot, force=force)
            else:
                db.publish_run_progress(run_id=run_id, progress=snapshot, force=force)

    def process(candidate: PersonalityCandidate, manager: GeminiRuntimeManager) -> None:
        checkpoint = db.get_personality_checkpoint(candidate.raw_name)
        attempts = _checkpoint_attempts_for_candidate(checkpoint, candidate)
        fingerprint = personality_source_fingerprint(candidate)
        model_input = preprocess_personality_name_for_model(candidate.raw_name)
        base = {"raw_name": candidate.raw_name, "source_fingerprint": fingerprint,
                "document_count": candidate.document_count, "mention_count": candidate.mention_count,
                "source_roles": list(candidate.roles), "prompt_version": PERSONALITY_NORMALIZATION_PROMPT_VERSION,
                "schema_version": SCHEMA_VERSION}
        log_message(f"library personalities: person start raw_name={candidate.raw_name}")
        db.save_personality_checkpoint(**base, state="processing", attempted_models=attempts,
            canonical_id=(checkpoint or {}).get("canonical_id"), retryable=bool((checkpoint or {}).get("retryable")))

        def call_model(model_name: str, api_key: str, _lease: Any) -> str:
            queue.model_attempts[model_name] += 1
            log_message(f"library personalities: model attempt raw_name={candidate.raw_name} model={model_name}")
            raw = request_json(api_key=api_key, model_name=model_name,
                               contents=[build_personality_normalization_prompt(model_input, document_languages=candidate.document_languages)],
                               response_schema=PersonalityResponse, timeout_seconds=config_integer("gemini", "request", "personality_timeout_seconds"))
            log_message(f"library personalities: model response raw_name={candidate.raw_name} model={model_name}\n"
                                  + format_personality_response_for_log(raw))
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
                                           failure_context=error, retryable=False,
                                           canonical_id=(checkpoint or {}).get("canonical_id"))

        state = "failed"
        retry = False
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
                                           failure_context=str(exc), retryable=False,
                                           canonical_id=(checkpoint or {}).get("canonical_id"))
        except GeminiModelPoolUnavailableError as exc:
            state = "deferred"
            db.save_personality_checkpoint(**base, state=state, attempted_models=attempts,
                                           failure_context=str(exc), retryable=True,
                                           canonical_id=(checkpoint or {}).get("canonical_id"))
            retry = exc.retry_at is not None
            if not retry and exc.all_models_unavailable:
                # No usable pool or known retry time: preserve untouched work.
                queue.outcome = "deferred"
        except GeminiModelPoolOperationalError as exc:
            state = "deferred" if exc.retryable else "failed"
            retry = exc.retryable
            if exc.model_name:
                previous = attempts.get(exc.model_name)
                attempts[exc.model_name] = {"kind": "transient", "error": str(exc)}
                if previous:
                    attempts[exc.model_name]["previous_failure"] = previous
            db.save_personality_checkpoint(**base, state=state, attempted_models=attempts,
                                           failure_context=str(exc), retryable=retry,
                                           canonical_id=(checkpoint or {}).get("canonical_id"))
            log_message(f"library personalities: person {state} raw_name={candidate.raw_name} reason={exc}")
        except GeminiStopRequestedError:
            if queue.outcome != "failed":
                queue.outcome = "stopped"
            # An existing deferred checkpoint stays durable across stop during retry.
            state = "deferred" if checkpoint and checkpoint.get("retryable") else "pending"
            db.save_personality_checkpoint(**base, state=state, attempted_models=attempts,
                canonical_id=(checkpoint or {}).get("canonical_id"), retryable=state == "deferred",
                failure_context=(checkpoint or {}).get("failure_context"))
        else:
            decision = result.value
            components = decision.person_components()
            state = "succeeded" if components is not None else decision.outcome
            if components is None:
                attempts[result.model_name] = {"kind": "decision", "outcome": decision.outcome,
                    "reason": decision.reason, "document_languages": list(candidate.document_languages)}
                db.save_personality_checkpoint(**base, state=state, attempted_models=attempts,
                                               failure_context=decision.reason, retryable=False, completed=True)
                log_message(f"library personalities: person decision raw_name={candidate.raw_name} outcome={state} reason={decision.reason}")
            else:
                try:
                    canonical = db.persist_personality_normalization(**base, components=storage_components(components),
                        display_name=build_canonical_name(components), identity_key=personality_identity_key(components), model=result.model_name)
                except CatalogConflict as exc:
                    state = "failed"
                    db.save_personality_checkpoint(**base, state=state, attempted_models=attempts,
                                                   failure_context=str(exc), retryable=False, domain_conflict=True,
                                           canonical_id=(checkpoint or {}).get("canonical_id"))
                    log_message(f"library personalities: identity conflict raw_name={candidate.raw_name} reason={exc}")
                else:
                    queue.model_successes[result.model_name] += 1
                    log_message(f"library personalities: person success raw_name={candidate.raw_name} entity_id={canonical['canonical_id']}")
        if retry and queue.turns[candidate.raw_name] == 1:
            log_message(f"library personalities: queue tail raw_name={candidate.raw_name} retry=1/1")
        queue.finish(candidate, state, retry=retry)
        publish()

    def work() -> None:
        manager = GeminiRuntimeManager(db, task_id=TASK_ID,
            should_stop=lambda: should_stop() or queue.outcome == "failed",
            pacing_policy=pacing_policy)
        while (candidate := queue.claim()) is not None:
            try:
                process(candidate, manager)
            except BaseException:
                queue.outcome = "failed"
                queue.states[candidate.raw_name] = "failed"
                queue.active -= 1
                raise

    publish()
    log_message(f"library personalities: start eligible={len(eligible)} total={len(source)}")
    try:
        work()
    finally:
        queue.states = {name: "deferred" if state == "retry_pending" else state for name, state in queue.states.items()}
        publish(force=True)
    snapshot = queue.snapshot(len(eligible))
    return {"kind": "library.personality_normalization_summary", **snapshot,
            "outcome": queue.outcome, "total": len(source), "processed": snapshot["processed"] + skipped,
            "skipped": skipped, "eligible_total": len(eligible),
            "remaining": len(eligible) - snapshot["resolved"]}


def execute(context: RunContext) -> dict[str, Any]:
    """Flow-owned handler for the transport-independent task runtime."""
    context.db.check_personality_catalog()
    if context.should_stop():
        return {"kind": "library.personality_normalization_summary", "outcome": "stopped"}
    models = load_required_gemini_model_pool()
    summary = run_personality_normalization(
        db=context.db, models=models, run_id=context.run_id, should_stop=context.should_stop,
        limit=context.options.limit,
        progress_sink=context.progress,
    )
    log_message(f"library personalities: final {json.dumps(summary, ensure_ascii=False, sort_keys=True)}")
    return summary
