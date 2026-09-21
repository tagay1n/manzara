"""Normalize exact raw bibliographic people into canonical personality records."""

# ruff: noqa: E402
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import sys
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
    PersonComponents,
    PersonalityCandidate,
    build_canonical_name,
    extract_personality_candidates,
    personality_identity_key,
    personality_source_fingerprint,
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
SCHEMA_VERSION = "person-components-v1"
_RESPONSE_LOG_MAX_CHARS = 8_000


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
        current = (
            checkpoint
            and checkpoint.get("source_fingerprint") == personality_source_fingerprint(candidate)
            and checkpoint.get("prompt_version") == PERSONALITY_NORMALIZATION_PROMPT_VERSION
            and checkpoint.get("schema_version") == SCHEMA_VERSION
        )
        if current and checkpoint.get("state") == "succeeded":
            skipped += 1
            continue
        if current and checkpoint.get("state") == "failed" and not checkpoint.get("retryable"):
            skipped += 1
            continue
        eligible.append(candidate)
    return eligible, skipped


def run_personality_normalization(
    *,
    db: Any,
    models: Sequence[str],
    run_id: int | None,
    should_stop: Callable[[], bool],
    request_json: Callable[..., str] = generate_structured_json,
    candidates: Sequence[PersonalityCandidate] | None = None,
    limit: int | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    """Run single-person structured requests, persisting after every person."""
    source_candidates = list(candidates) if candidates is not None else extract_personality_candidates(db.list_personality_source_documents())
    if workers > 1 and len(source_candidates) > 1:
        count = min(int(workers), len(source_candidates))
        partitions = [source_candidates[index::count] for index in range(count)]
        with ThreadPoolExecutor(max_workers=count, thread_name_prefix="personalities-worker") as executor:
            summaries = list(executor.map(lambda partition: run_personality_normalization(
                db=db, models=models, run_id=run_id, should_stop=should_stop, request_json=request_json,
                candidates=partition, limit=None, workers=1), partitions))
        totals: Counter[str] = Counter()
        attempts: Counter[str] = Counter()
        successes: Counter[str] = Counter()
        for summary in summaries:
            totals.update({key: int(summary.get(key) or 0) for key in ("processed", "succeeded", "skipped", "deferred", "failed")})
            attempts.update(summary.get("model_attempts") or {})
            successes.update(summary.get("model_successes") or {})
        return {"kind": "library.personality_normalization_summary", "outcome": next((item.get("outcome") for item in summaries if item.get("outcome") != "completed"), "completed"), "total": len(source_candidates), **dict(totals), "model_attempts": dict(attempts), "model_successes": dict(successes), "workers": count}
    eligible, skipped = _eligible_candidates(source_candidates, db.list_personality_checkpoints())
    if limit is not None:
        eligible = eligible[:max(0, int(limit))]
    counters: Counter[str] = Counter(skipped=skipped)
    model_attempts: Counter[str] = Counter()
    model_successes: Counter[str] = Counter()
    worker_id = current_gemini_worker_id("personalities")
    manager = GeminiRuntimeManager(db, task_id=TASK_ID, panel_id=PANEL_ID, should_stop=should_stop, worker_id=worker_id)

    def progress() -> None:
        processed = sum(counters[key] for key in ("succeeded", "skipped", "deferred", "failed"))
        payload = {
            "current": processed, "total": len(source_candidates), "processed": processed,
            "succeeded": counters["succeeded"], "skipped": counters["skipped"],
            "deferred": counters["deferred"], "failed": counters["failed"],
            "model_attempts": dict(model_attempts), "model_successes": dict(model_successes),
        }
        if run_id is not None:
            db.publish_run_progress(task_id=TASK_ID, run_id=run_id, panel_id=PANEL_ID, progress=payload)

    progress()
    emit_gemini_worker_log(f"library personalities: start eligible={len(eligible)} total={len(source_candidates)}", worker_id=worker_id)
    outcome = "completed"
    for candidate in eligible:
        if should_stop():
            outcome = "stopped"
            break
        checkpoint = db.get_personality_checkpoint(candidate.raw_name)
        attempts = _checkpoint_attempts(checkpoint)
        fingerprint = personality_source_fingerprint(candidate)
        emit_gemini_worker_log(f"library personalities: person start raw_name={candidate.raw_name}", worker_id=worker_id)

        def call_model(model_name: str, api_key: str, _lease: Any) -> str:
            model_attempts[model_name] += 1
            emit_gemini_worker_log(f"library personalities: model attempt raw_name={candidate.raw_name} model={model_name}", worker_id=worker_id)
            response = request_json(api_key=api_key, model_name=model_name,
                contents=[build_personality_normalization_prompt(
                    candidate.raw_name, document_languages=candidate.document_languages
                )],
                response_schema=PersonComponents)
            emit_gemini_worker_log(
                f"library personalities: model response raw_name={candidate.raw_name} model={model_name}\n"
                + format_personality_response_for_log(response),
                worker_id=worker_id,
            )
            return response

        def parse(raw: str) -> PersonComponents:
            try:
                return PersonComponents.model_validate_json(raw)
            except Exception as exc:  # malformed output is a content failure for this model
                raise GeminiModelResponseError(str(exc)) from exc

        def record_failure(model_name: str, kind: str, error: str) -> None:
            attempts[model_name] = {"kind": kind, "error": error}
            db.save_personality_checkpoint(raw_name=candidate.raw_name, source_fingerprint=fingerprint,
                document_count=candidate.document_count, mention_count=candidate.mention_count,
                source_roles=list(candidate.roles), prompt_version=PERSONALITY_NORMALIZATION_PROMPT_VERSION,
                schema_version=SCHEMA_VERSION, state="processing", attempted_models=attempts,
                failure_context=error, retryable=False)

        try:
            result = run_ordered_model_pool(manager=manager, models=models, request=call_model,
                parse=parse, record_failure=record_failure, run_id=run_id,
                already_attempted=attempts.keys())
        except GeminiModelPoolExhaustedError as exc:
            db.save_personality_checkpoint(raw_name=candidate.raw_name, source_fingerprint=fingerprint,
                document_count=candidate.document_count, mention_count=candidate.mention_count,
                source_roles=list(candidate.roles), prompt_version=PERSONALITY_NORMALIZATION_PROMPT_VERSION,
                schema_version=SCHEMA_VERSION, state="failed", attempted_models=attempts,
                failure_context=str(exc), retryable=False)
            counters["failed"] += 1
        except GeminiModelPoolItemRejectedError as exc:
            db.save_personality_checkpoint(raw_name=candidate.raw_name, source_fingerprint=fingerprint,
                document_count=candidate.document_count, mention_count=candidate.mention_count,
                source_roles=list(candidate.roles), prompt_version=PERSONALITY_NORMALIZATION_PROMPT_VERSION,
                schema_version=SCHEMA_VERSION, state="failed", attempted_models=attempts,
                failure_context=str(exc), retryable=False)
            counters["failed"] += 1
        except (GeminiModelPoolUnavailableError, GeminiModelPoolOperationalError) as exc:
            db.save_personality_checkpoint(raw_name=candidate.raw_name, source_fingerprint=fingerprint,
                document_count=candidate.document_count, mention_count=candidate.mention_count,
                source_roles=list(candidate.roles), prompt_version=PERSONALITY_NORMALIZATION_PROMPT_VERSION,
                schema_version=SCHEMA_VERSION, state="deferred", attempted_models=attempts,
                failure_context=str(exc), retryable=True)
            counters["deferred"] += 1
        except GeminiStopRequestedError:
            outcome = "stopped"
            break
        else:
            components = result.value
            canonical = db.persist_personality_normalization(raw_name=candidate.raw_name,
                source_fingerprint=fingerprint, document_count=candidate.document_count,
                mention_count=candidate.mention_count, source_roles=list(candidate.roles),
                components=storage_components(components), display_name=build_canonical_name(components),
                identity_key=personality_identity_key(components), model=result.model_name,
                prompt_version=PERSONALITY_NORMALIZATION_PROMPT_VERSION, schema_version=SCHEMA_VERSION)
            model_successes[result.model_name] += 1
            counters["succeeded"] += 1
            emit_gemini_worker_log(f"library personalities: person success raw_name={candidate.raw_name} canonical_id={canonical['canonical_id']}", worker_id=worker_id)
        progress()
    summary = {"kind": "library.personality_normalization_summary", "outcome": outcome,
        "total": len(source_candidates), "processed": sum(counters.values()), **dict(counters),
        "model_attempts": dict(model_attempts), "model_successes": dict(model_successes)}
    return summary


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
