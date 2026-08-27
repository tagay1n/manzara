"""Resumable adaptive Gemini validation for collection proposals."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from google import genai
from google.genai import types
from sqlalchemy import text

from app.db import Database
from app.gemini_config import load_required_gemini_model_pool
from app.gemini_runtime import (
    GeminiAllKeysExhaustedError,
    GeminiQuotaExceededError,
    GeminiRequestRejectedError,
    GeminiRuntimeError,
    GeminiRuntimeManager,
    GeminiServerPauseError,
    GeminiStopRequestedError,
)
from app.modules.library.collection_detection import (
    AdaptiveBatchSizer,
    PROMPT_VERSION,
    parse_validation_response,
)
from app.modules.library.collection_constants import COLLECTIONS_PANEL_ID
from app.modules.library.stats import create_runtime_engine


TASK_ID = "library.collection_validate"
PANEL_ID = COLLECTIONS_PANEL_ID


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_search_path(conn: Any) -> None:
    import os
    import re

    schema = (
        str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip() or "monocorpus"
    )
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        schema = "monocorpus"
    conn.execute(text(f'SET search_path TO "{schema}", public'))


def build_validation_prompt(
    proposal: Mapping[str, Any],
    documents: list[Mapping[str, Any]],
    *,
    excerpts: Mapping[str, str] | None = None,
) -> str:
    evidence_docs = []
    excerpts = excerpts or {}
    for item in documents:
        md5 = str(item["md5"])
        evidence_docs.append(
            {
                "md5": md5,
                "title": item.get("title"),
                "type": item.get("work_type"),
                "date": item.get("publication_date"),
                "issue_number": item.get("issue_number"),
                "publishers": item.get("publishers_json") or [],
                "authors": item.get("authors_json") or [],
                "genres": item.get("genres_json") or [],
                "description": item.get("description") or "",
                "excerpt": excerpts.get(md5) or None,
            }
        )
    payload = {
        "candidate_type": proposal["proposal_type"],
        "proposed_collection_name": proposal["proposed_title"],
        "known_collection_name": proposal.get("target_title"),
        "aggregate_evidence": proposal.get("evidence_json") or {},
        "documents": evidence_docs,
    }
    return (
        "You verify bibliographic collections for a Tatar-language digital library.\n"
        "A collection must be an explicitly named recurring publication or a named document/book series. "
        "Shared topic, author, publisher, language, edition, or translation alone is not a collection. "
        "Metadata may contain OCR errors. Do not decide library value and do not rewrite metadata. "
        "Source paths are intentionally unavailable.\n"
        "Return JSON only. Decide whether the candidate is one named collection, provide its canonical name, "
        "and classify every supplied MD5 exactly once as belongs, does_not_belong, or uncertain. "
        "Give short evidence and confidence from 0 to 1.\n\nINPUT:\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


_RESPONSE_SCHEMA = {
    "type": "object",
    "required": [
        "is_named_collection",
        "canonical_name",
        "confidence",
        "rationale",
        "documents",
    ],
    "properties": {
        "is_named_collection": {"type": "boolean"},
        "canonical_name": {"type": "string"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["md5", "verdict", "confidence", "rationale"],
                "properties": {
                    "md5": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["belongs", "does_not_belong", "uncertain"],
                    },
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}


def _gemini_call(api_key: str, model_name: str, prompt: str) -> dict[str, Any]:
    response = genai.Client(api_key=api_key).models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_json_schema=_RESPONSE_SCHEMA,
            http_options=types.HttpOptions(timeout=180_000),
        ),
    )
    raw = str(response.text or "").strip()
    return json.loads(raw)


def _attempt_hash(
    proposal_id: int, model: str, documents: list[Mapping[str, Any]]
) -> str:
    payload = [PROMPT_VERSION, str(proposal_id), model]
    payload.extend(f"{item['md5']}:{item['input_hash']}" for item in documents)
    return hashlib.sha256("|".join(payload).encode("utf-8")).hexdigest()


def _record_attempt(
    conn: Any,
    *,
    proposal_id: int,
    run_id: int,
    model: str,
    documents: list[Mapping[str, Any]],
    status: str,
    started: float,
    response: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> None:
    now = _now()
    conn.execute(
        text(
            """
            INSERT INTO library_collection_validation_attempts (
                proposal_id,run_id,model_name,prompt_version,input_hash,
                requested_md5_json,batch_size,status,response_json,error_text,
                latency_ms,created_at,completed_at
            ) VALUES (
                :proposal_id,:run_id,:model,:prompt_version,:input_hash,
                CAST(:md5s AS JSONB),:batch_size,:status,CAST(:response AS JSONB),:error,
                :latency_ms,:created_at,:completed_at
            )
            """
        ),
        {
            "proposal_id": proposal_id,
            "run_id": run_id,
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "input_hash": _attempt_hash(proposal_id, model, documents),
            "md5s": json.dumps([item["md5"] for item in documents]),
            "batch_size": len(documents),
            "status": status,
            "response": json.dumps(response, ensure_ascii=False)
            if response is not None
            else None,
            "error": error,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "created_at": now,
            "completed_at": now,
        },
    )


def _publish_progress(
    db: Database, run_id: int, counters: Counter[str], **extra: Any
) -> None:
    payload = {"status": "running", **dict(counters), **extra}
    db.update_run_progress(run_id, payload)
    db.insert_event(
        "task.progress",
        task_id=TASK_ID,
        run_id=run_id,
        panel_id=PANEL_ID,
        payload={"status": "running", "progress": payload},
    )


def _validate_collection_proposals_worker(
    db: Database,
    *,
    run_id: int,
    should_stop: Callable[[], bool],
    excerpt_loader: Callable[[str], str | None] | None = None,
    call_gemini: Callable[[str, str, str], dict[str, Any]] = _gemini_call,
    workers: int = 1,
    recover_unfinished: bool = True,
) -> dict[str, Any]:
    models = load_required_gemini_model_pool()
    manager = GeminiRuntimeManager(
        db,
        task_id=TASK_ID,
        panel_id=PANEL_ID,
        should_stop=should_stop,
    )
    sizer = AdaptiveBatchSizer(initial_size=20)
    counters = Counter()
    exhausted_models: set[str] = set()
    all_models_exhausted = False
    engine, _ = create_runtime_engine()
    try:
        with engine.begin() as conn:
            _set_search_path(conn)
            recent_sizes = (
                conn.execute(
                    text(
                        """
                    SELECT DISTINCT ON (model_name) model_name, batch_size, status
                    FROM library_collection_validation_attempts
                    ORDER BY model_name, attempt_id DESC
                    """
                    )
                )
                .mappings()
                .all()
            )
            for recent in recent_sizes:
                size = int(recent["batch_size"] or 20)
                if recent["status"] == "malformed_or_timeout":
                    size = max(1, size // 2)
                sizer.set_size(str(recent["model_name"]), size)
            if recover_unfinished:
                conn.execute(
                    text(
                        "UPDATE library_collection_proposals SET status='queued_validation',updated_at=:now WHERE status='validating'"
                    ),
                    {"now": _now()},
                )

        while not should_stop():
            with engine.begin() as conn:
                _set_search_path(conn)
                proposal = (
                    conn.execute(
                        text(
                            """
                        SELECT p.*, c.title AS target_title
                        FROM library_collection_proposals p
                        LEFT JOIN library_collections c ON c.collection_id=p.target_collection_id
                        WHERE p.status='queued_validation'
                        ORDER BY CASE WHEN p.proposal_type='attach_to_collection' THEN 0 ELSE 1 END,
                                 p.updated_at, p.proposal_id
                        FOR UPDATE OF p SKIP LOCKED LIMIT 1
                        """
                        )
                    )
                    .mappings()
                    .first()
                )
                if not proposal:
                    break
                conn.execute(
                    text(
                        "UPDATE library_collection_proposals SET status='validating',updated_at=:now WHERE proposal_id=:id"
                    ),
                    {"now": _now(), "id": proposal["proposal_id"]},
                )
            proposal = dict(proposal)
            counters["proposals_started"] += 1
            _publish_progress(
                db,
                run_id,
                counters,
                proposal_id=int(proposal["proposal_id"]),
                adaptive_batch_sizes={model: sizer.size_for(model) for model in models},
            )
            model_order = list(models)
            rotate_models: set[str] = set()
            while not should_stop():
                with engine.connect() as conn:
                    _set_search_path(conn)
                    pending = (
                        conn.execute(
                            text(
                                """
                            SELECT pi.md5,pi.input_hash,pi.deterministic_score,
                                   f.title,f.work_type,f.publication_date,f.issue_number,
                                   f.publishers_json,f.authors_json,f.genres_json,f.description
                            FROM library_collection_proposal_items pi
                            JOIN library_collection_document_features f ON f.md5=pi.md5
                            WHERE pi.proposal_id=:id AND pi.validated_at IS NULL
                            ORDER BY pi.deterministic_score DESC,pi.md5
                            """
                            ),
                            {"id": proposal["proposal_id"]},
                        )
                        .mappings()
                        .all()
                    )
                if not pending:
                    break
                available_models = [
                    model
                    for model in model_order
                    if model not in rotate_models and model not in exhausted_models
                ]
                if not available_models:
                    if len(exhausted_models) == len(models):
                        counters["all_models_exhausted"] += 1
                        all_models_exhausted = True
                        break
                    # A quota response exhausted the attempted key, not necessarily
                    # every key for the model. Rotate once across the pool, then retry.
                    rotate_models.clear()
                    continue
                model = available_models[counters["requests"] % len(available_models)]
                batch = [dict(item) for item in pending[: sizer.size_for(model)]]
                excerpts: dict[str, str] = {}
                if excerpt_loader:
                    for item in batch:
                        if float(item.get("deterministic_score") or 0) < 0.85:
                            excerpt = excerpt_loader(str(item["md5"]))
                            if excerpt:
                                excerpts[str(item["md5"])] = excerpt[:2000]
                prompt = build_validation_prompt(proposal, batch, excerpts=excerpts)
                parsed: dict[str, Any] | None = None
                malformed_error: str | None = None
                request_rejected = False
                service_deferred = False
                for content_attempt in range(2):
                    started = time.monotonic()
                    try:
                        raw = manager.run_with_key(
                            model_name=model,
                            run_id=run_id,
                            max_attempts=2,
                            call=lambda key, _lease: call_gemini(key, model, prompt),
                        )
                        parsed = parse_validation_response(
                            raw, requested_md5s=[item["md5"] for item in batch]
                        )
                        with engine.begin() as conn:
                            _set_search_path(conn)
                            _record_attempt(
                                conn,
                                proposal_id=int(proposal["proposal_id"]),
                                run_id=run_id,
                                model=model,
                                documents=batch,
                                status="success",
                                started=started,
                                response=parsed,
                            )
                        break
                    except GeminiAllKeysExhaustedError as exc:
                        exhausted_models.add(model)
                        counters["models_exhausted"] += 1
                        with engine.begin() as conn:
                            _set_search_path(conn)
                            _record_attempt(
                                conn,
                                proposal_id=int(proposal["proposal_id"]),
                                run_id=run_id,
                                model=model,
                                documents=batch,
                                status="all_keys_exhausted",
                                started=started,
                                error=str(exc),
                            )
                        break
                    except GeminiQuotaExceededError as exc:
                        rotate_models.add(model)
                        counters["quota_errors"] += 1
                        with engine.begin() as conn:
                            _set_search_path(conn)
                            _record_attempt(
                                conn,
                                proposal_id=int(proposal["proposal_id"]),
                                run_id=run_id,
                                model=model,
                                documents=batch,
                                status="quota",
                                started=started,
                                error=str(exc),
                            )
                        break
                    except GeminiRequestRejectedError as exc:
                        request_rejected = True
                        malformed_error = str(exc)
                        counters["request_rejected"] += 1
                        with engine.begin() as conn:
                            _set_search_path(conn)
                            _record_attempt(
                                conn,
                                proposal_id=int(proposal["proposal_id"]),
                                run_id=run_id,
                                model=model,
                                documents=batch,
                                status="request_rejected",
                                started=started,
                                error=str(exc),
                            )
                        break
                    except GeminiServerPauseError as exc:
                        service_deferred = True
                        malformed_error = str(exc)
                        counters["service_deferred"] += 1
                        with engine.begin() as conn:
                            _set_search_path(conn)
                            _record_attempt(
                                conn,
                                proposal_id=int(proposal["proposal_id"]),
                                run_id=run_id,
                                model=model,
                                documents=batch,
                                status="service_deferred",
                                started=started,
                                error=str(exc),
                            )
                        break
                    except GeminiStopRequestedError:
                        break
                    except (
                        GeminiRuntimeError,
                        TimeoutError,
                        ValueError,
                        json.JSONDecodeError,
                    ) as exc:
                        malformed_error = f"{type(exc).__name__}: {exc}"
                        counters["malformed_or_timeout"] += 1
                        with engine.begin() as conn:
                            _set_search_path(conn)
                            _record_attempt(
                                conn,
                                proposal_id=int(proposal["proposal_id"]),
                                run_id=run_id,
                                model=model,
                                documents=batch,
                                status="malformed_or_timeout",
                                started=started,
                                error=malformed_error,
                            )
                        if content_attempt == 0:
                            continue
                if should_stop():
                    break
                if service_deferred:
                    break
                if model in rotate_models or model in exhausted_models:
                    continue
                if parsed is None:
                    if not request_rejected and malformed_error and len(batch) > 1:
                        sizer.record_failure(model)
                        continue
                    with engine.begin() as conn:
                        _set_search_path(conn)
                        for item in batch:
                            conn.execute(
                                text(
                                    "UPDATE library_collection_proposal_items SET gemini_verdict='uncertain',gemini_confidence=0,gemini_rationale=:error,gemini_model=:model,prompt_version=:prompt,validated_at=:now WHERE proposal_id=:id AND md5=:md5"
                                ),
                                {
                                    "error": malformed_error or "Validation failed",
                                    "model": model,
                                    "prompt": PROMPT_VERSION,
                                    "now": _now(),
                                    "id": proposal["proposal_id"],
                                    "md5": item["md5"],
                                },
                            )
                    counters["items_failed"] += len(batch)
                    _publish_progress(
                        db, run_id, counters, proposal_id=int(proposal["proposal_id"])
                    )
                    continue

                sizer.record_success(model)
                counters["requests"] += 1
                with engine.begin() as conn:
                    _set_search_path(conn)
                    conn.execute(
                        text(
                            """
                            UPDATE library_collection_proposals
                            SET gemini_collection_verdict = CASE
                                    WHEN gemini_collection_verdict IS TRUE OR :verdict IS TRUE THEN TRUE
                                    ELSE FALSE
                                END,
                                gemini_canonical_name = CASE
                                    WHEN :verdict IS TRUE AND COALESCE(:name, '') <> ''
                                    THEN :name
                                    ELSE gemini_canonical_name
                                END,
                                gemini_confidence = GREATEST(
                                    COALESCE(gemini_confidence, 0), :confidence
                                ),
                                gemini_rationale = CASE
                                    WHEN :verdict IS TRUE THEN :rationale
                                    ELSE COALESCE(gemini_rationale, :rationale)
                                END,
                                updated_at=:now
                            WHERE proposal_id=:id
                            """
                        ),
                        {
                            "verdict": parsed["is_named_collection"],
                            "name": parsed["canonical_name"],
                            "confidence": parsed["confidence"],
                            "rationale": parsed["rationale"],
                            "now": _now(),
                            "id": proposal["proposal_id"],
                        },
                    )
                    for result in parsed["documents"]:
                        conn.execute(
                            text(
                                "UPDATE library_collection_proposal_items SET gemini_verdict=:verdict,gemini_confidence=:confidence,gemini_rationale=:rationale,gemini_model=:model,prompt_version=:prompt,validated_at=:now WHERE proposal_id=:id AND md5=:md5"
                            ),
                            {
                                **result,
                                "model": model,
                                "prompt": PROMPT_VERSION,
                                "now": _now(),
                                "id": proposal["proposal_id"],
                            },
                        )
                        counters[f"items_{result['verdict']}"] += 1
                _publish_progress(
                    db,
                    run_id,
                    counters,
                    proposal_id=int(proposal["proposal_id"]),
                    model=model,
                    adaptive_batch_sizes={
                        name: sizer.size_for(name) for name in models
                    },
                )

            with engine.begin() as conn:
                _set_search_path(conn)
                remaining = conn.execute(
                    text(
                        "SELECT COUNT(*) FROM library_collection_proposal_items WHERE proposal_id=:id AND validated_at IS NULL"
                    ),
                    {"id": proposal["proposal_id"]},
                ).scalar_one()
                if should_stop() or remaining:
                    status = "queued_validation"
                elif proposal.get("gemini_collection_verdict") is False:
                    status = "ai_dismissed"
                else:
                    status = "review_ready"
                # Read the latest persisted group verdict rather than the stale claimed row.
                latest = conn.execute(
                    text(
                        "SELECT gemini_collection_verdict FROM library_collection_proposals WHERE proposal_id=:id"
                    ),
                    {"id": proposal["proposal_id"]},
                ).scalar_one()
                if not remaining and latest is False:
                    status = "ai_dismissed"
                elif not remaining:
                    status = "review_ready"
                conn.execute(
                    text(
                        "UPDATE library_collection_proposals SET status=:status,updated_at=:now WHERE proposal_id=:id"
                    ),
                    {"status": status, "now": _now(), "id": proposal["proposal_id"]},
                )
                counters[f"proposals_{status}"] += 1
            if should_stop():
                break
            if all_models_exhausted:
                break

        summary = {
            "kind": "library.collection_validation_summary",
            "stopped": bool(should_stop()),
            "workers": int(workers),
            **dict(counters),
        }
        db.update_run_progress(
            run_id,
            {"status": "stopped" if summary["stopped"] else "completed", **summary},
        )
        db.insert_event(
            "task.progress",
            task_id=TASK_ID,
            run_id=run_id,
            panel_id=PANEL_ID,
            payload={
                "status": "stopped" if summary["stopped"] else "completed",
                "progress": summary,
            },
        )
        return summary
    finally:
        engine.dispose()


def validate_collection_proposals(
    db: Database,
    *,
    run_id: int,
    should_stop: Callable[[], bool],
    excerpt_loader: Callable[[str], str | None] | None = None,
    call_gemini: Callable[[str, str, str], dict[str, Any]] = _gemini_call,
    workers: int = 1,
) -> dict[str, Any]:
    """Run proposal workers; row locking guarantees distinct proposal claims."""
    worker_count = max(1, int(workers))
    if worker_count == 1:
        return _validate_collection_proposals_worker(
            db,
            run_id=run_id,
            should_stop=should_stop,
            excerpt_loader=excerpt_loader,
            call_gemini=call_gemini,
            workers=1,
            recover_unfinished=True,
        )
    print(
        f"library collection validation: worker pool requested={workers} started={worker_count}",
        flush=True,
    )
    recovery_engine, _ = create_runtime_engine()
    try:
        with recovery_engine.begin() as conn:
            _set_search_path(conn)
            conn.execute(
                text(
                    "UPDATE library_collection_proposals SET status='queued_validation',updated_at=:now WHERE status='validating'"
                ),
                {"now": _now()},
            )
    finally:
        recovery_engine.dispose()
    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="collection-worker"
    ) as executor:
        futures = [
            executor.submit(
                _validate_collection_proposals_worker,
                db,
                run_id=run_id,
                should_stop=should_stop,
                excerpt_loader=excerpt_loader,
                call_gemini=call_gemini,
                workers=1,
                recover_unfinished=False,
            )
            for _index in range(worker_count)
        ]
        results = [future.result() for future in futures]
    counters = Counter()
    for result in results:
        counters.update(
            {
                key: int(value)
                for key, value in result.items()
                if key not in {"kind", "stopped", "workers"}
                and isinstance(value, int)
            }
        )
    summary = {
        "kind": "library.collection_validation_summary",
        "stopped": bool(should_stop()),
        "workers": worker_count,
        **dict(counters),
    }
    db.update_run_progress(
        run_id,
        {"status": "stopped" if summary["stopped"] else "completed", **summary},
    )
    return summary


__all__ = ["build_validation_prompt", "validate_collection_proposals"]
