"""Read-only local-LLM benchmark for Library collection candidates."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from sqlalchemy import Engine, text

from app.modules.library.collections import _build_collection_review_payload


PROMPT_VERSION = "collection-triage-v1"
_VERDICTS = {"approve", "reject", "uncertain"}


class TriageRepository(Protocol):
    def list_gold_examples(self) -> list[dict[str, Any]]: ...

    def find_cached_evaluation(self, **identity: Any) -> dict[str, Any] | None: ...

    def save_evaluation(self, evaluation: dict[str, Any]) -> None: ...


class TriageClient(Protocol):
    def evaluate(self, *, model_name: str, prompt: str) -> dict[str, Any]: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_review_item(item: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "title",
        "file_name",
        "parent",
        "publisher",
        "genre",
        "work_type",
        "publication_date",
        "issue_number",
        "reasons",
    )
    return {key: item.get(key) for key in allowed if item.get(key) not in (None, "", [])}


def build_triage_input(example: Mapping[str, Any]) -> dict[str, Any]:
    """Build bounded model evidence without exposing the human verdict."""
    evidence = example.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("Collection triage example is missing evidence")
    collection = evidence.get("collection")
    if not isinstance(collection, Mapping):
        collection = {}
    return {
        "title": str(collection.get("title") or ""),
        "normalized_title": str(collection.get("normalized_title") or ""),
        "item_count": int(collection.get("item_count") or 0),
        "detection_confidence": float(collection.get("confidence") or 0.0),
        "summary": evidence.get("summary") if isinstance(evidence.get("summary"), Mapping) else {},
        "grouping_evidence": (
            evidence.get("grouping_evidence")
            if isinstance(evidence.get("grouping_evidence"), list)
            else []
        ),
        "consistency": (
            evidence.get("consistency") if isinstance(evidence.get("consistency"), Mapping) else {}
        ),
        "outliers": [
            _safe_review_item(item)
            for item in (evidence.get("outliers") or [])[:20]
            if isinstance(item, Mapping)
        ],
        "samples": [
            _safe_review_item(item)
            for item in (evidence.get("samples") or [])[:8]
            if isinstance(item, Mapping)
        ],
    }


def build_triage_prompt(example: Mapping[str, Any]) -> str:
    """Build the stable zero-shot prompt used by every benchmarked model."""
    evidence_json = json.dumps(
        build_triage_input(example),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "You review proposed groups of Tatar-language documents for an online library. "
        "Decide whether the documents form one real collection such as issues of the same "
        "newspaper, journal, magazine, or book series. A shared folder alone is not enough. "
        "OCR spelling differences and Tatar/Russian title variants may still describe one "
        "collection. Return uncertain when the evidence is insufficient. Do not invent facts.\n\n"
        "Return one JSON object with exactly these fields: verdict (approve, reject, or uncertain), "
        "confidence (number from 0 to 1), rationale (short text), signals (array of short texts).\n\n"
        f"Evidence:\n{evidence_json}"
    )


def parse_triage_response(raw: str) -> dict[str, Any]:
    """Validate one model response without silently coercing ambiguous values."""
    value = str(raw or "").strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        value = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model response is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Model response must be a JSON object")
    if set(payload) != {"verdict", "confidence", "rationale", "signals"}:
        raise ValueError("Model response has unexpected or missing fields")
    verdict = payload.get("verdict")
    if not isinstance(verdict, str) or verdict not in _VERDICTS:
        raise ValueError("verdict must be approve, reject, or uncertain")
    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be numeric")
    confidence = float(confidence)
    if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
        raise ValueError("confidence must be between 0 and 1")
    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("rationale must be non-empty text")
    signals = payload.get("signals")
    if not isinstance(signals, list) or len(signals) > 8:
        raise ValueError("signals must be an array with at most 8 items")
    normalized_signals: list[str] = []
    for signal in signals:
        if not isinstance(signal, str) or not signal.strip():
            raise ValueError("every signal must be non-empty text")
        normalized_signals.append(signal.strip()[:300])
    return {
        "verdict": verdict,
        "confidence": confidence,
        "rationale": rationale.strip()[:2000],
        "signals": normalized_signals,
    }


def _percent(numerator: int, denominator: int) -> float:
    return round((numerator / denominator) * 100, 2) if denominator else 0.0


def _class_metrics(rows: Sequence[Mapping[str, Any]], label: str) -> dict[str, float | int]:
    true_positive = sum(
        1 for row in rows if row.get("gold_verdict") == label and row.get("verdict") == label
    )
    false_positive = sum(
        1 for row in rows if row.get("gold_verdict") != label and row.get("verdict") == label
    )
    false_negative = sum(
        1 for row in rows if row.get("gold_verdict") == label and row.get("verdict") != label
    )
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def calculate_model_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Calculate conservative metrics where abstentions and parse failures are incorrect."""
    total = len(rows)
    confusion = {
        gold: {prediction: 0 for prediction in ("approve", "reject", "uncertain", "parse_failed")}
        for gold in ("approve", "reject")
    }
    for row in rows:
        gold = str(row.get("gold_verdict") or "")
        if gold not in confusion:
            continue
        prediction = str(row.get("verdict") or "")
        if str(row.get("status") or "") != "completed":
            prediction = "parse_failed"
        if prediction not in confusion[gold]:
            prediction = "parse_failed"
        confusion[gold][prediction] += 1

    completed = sum(1 for row in rows if row.get("status") == "completed")
    covered = sum(1 for row in rows if row.get("verdict") in {"approve", "reject"})
    correct = sum(
        1
        for row in rows
        if row.get("status") == "completed" and row.get("verdict") == row.get("gold_verdict")
    )
    by_class = {label: _class_metrics(rows, label) for label in ("approve", "reject")}
    latencies = sorted(max(0, int(row.get("latency_ms") or 0)) for row in rows)
    p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1) if latencies else 0
    macro_f1 = round(sum(float(item["f1"]) for item in by_class.values()) / 2, 4)
    return {
        "total": total,
        "accuracy": _percent(correct, total),
        "macro_f1": macro_f1,
        "coverage": _percent(covered, total),
        "structured_success": _percent(completed, total),
        "confusion": confusion,
        "classes": by_class,
        "latency_ms": {
            "average": round(sum(latencies) / len(latencies)) if latencies else 0,
            "p95": latencies[p95_index] if latencies else 0,
        },
        "passes_ui_gate": (
            macro_f1 >= 0.85
            and float(by_class["approve"]["precision"]) >= 0.90
            and float(by_class["reject"]["precision"]) >= 0.90
            and _percent(covered, total) >= 60.0
            and _percent(completed, total) >= 99.0
        ),
    }


def _input_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _evaluate_one(
    *,
    repository: TriageRepository,
    client: TriageClient,
    example: Mapping[str, Any],
    model_name: str,
    run_id: int,
) -> tuple[dict[str, Any], bool]:
    collection_id = int(example.get("collection_id") or 0)
    prompt = build_triage_prompt(example)
    input_hash = _input_hash(prompt)
    identity = {
        "collection_id": collection_id,
        "model_name": model_name,
        "prompt_version": PROMPT_VERSION,
        "input_hash": input_hash,
    }
    cached = repository.find_cached_evaluation(**identity)
    if cached:
        row = dict(cached)
        row["gold_verdict"] = str(example.get("gold_verdict") or "")
        return row, True

    started_at = _utc_now()
    raw_response = ""
    latency_ms = 0
    tokens = {"prompt_tokens": 0, "output_tokens": 0}
    status = "completed"
    error_text = None
    parsed: dict[str, Any] = {}
    try:
        response = client.evaluate(model_name=model_name, prompt=prompt)
        raw_response = str(response.get("content") or "")
        latency_ms += int(response.get("latency_ms") or 0)
        tokens = {
            "prompt_tokens": int(response.get("prompt_tokens") or 0),
            "output_tokens": int(response.get("output_tokens") or 0),
        }
        try:
            parsed = parse_triage_response(raw_response)
        except ValueError as first_error:
            repair_prompt = (
                f"{prompt}\n\nYour previous response was invalid: {first_error}. "
                "Return only the required JSON object."
            )
            response = client.evaluate(model_name=model_name, prompt=repair_prompt)
            raw_response = str(response.get("content") or "")
            latency_ms += int(response.get("latency_ms") or 0)
            tokens["prompt_tokens"] += int(response.get("prompt_tokens") or 0)
            tokens["output_tokens"] += int(response.get("output_tokens") or 0)
            parsed = parse_triage_response(raw_response)
    except ValueError as exc:
        status = "parse_failed"
        error_text = str(exc)
    except Exception as exc:  # noqa: BLE001
        status = "request_failed"
        error_text = str(exc)

    evaluation = {
        **identity,
        "run_id": int(run_id),
        "gold_verdict": str(example.get("gold_verdict") or ""),
        "verdict": parsed.get("verdict"),
        "confidence": parsed.get("confidence"),
        "rationale": parsed.get("rationale"),
        "signals": parsed.get("signals", []),
        "raw_response": raw_response,
        "status": status,
        "error_text": error_text,
        "latency_ms": latency_ms,
        **tokens,
        "started_at": started_at,
        "completed_at": _utc_now(),
    }
    repository.save_evaluation(evaluation)
    return evaluation, False


def run_collection_triage_benchmark(
    *,
    repository: TriageRepository,
    client: TriageClient,
    models: tuple[str, ...],
    run_id: int,
    publish_progress: Callable[[dict[str, Any]], None],
    should_stop: Callable[[], bool],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Evaluate reviewed collections serially with resumable per-model checkpoints."""
    if not models:
        raise ValueError("At least one local model is required")
    examples = repository.list_gold_examples()
    gold_labels = {str(example.get("gold_verdict") or "") for example in examples}
    if not examples or not gold_labels.issubset({"approve", "reject"}):
        raise RuntimeError("Collection triage gold set is empty or invalid")
    total = len(examples) * len(models)
    processed = 0
    reused = 0
    stopped = False
    results_by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    report_evaluations: list[dict[str, Any]] = []

    publish_progress(
        {
            "current": 0,
            "total": total,
            "percent": 0.0 if total else 100.0,
            "model": models[0],
            "collection_id": None,
            "reused": 0,
            "failed": 0,
        }
    )

    for model_name in models:
        for example in examples:
            if should_stop():
                stopped = True
                break
            evaluation, was_reused = _evaluate_one(
                repository=repository,
                client=client,
                example=example,
                model_name=model_name,
                run_id=run_id,
            )
            processed += 1
            reused += int(was_reused)
            results_by_model[model_name].append(evaluation)
            report_evaluations.append({**evaluation, "reused": was_reused})
            progress = {
                "current": processed,
                "total": total,
                "percent": round((processed / total) * 100, 2) if total else 100.0,
                "model": model_name,
                "collection_id": int(example.get("collection_id") or 0),
                "reused": reused,
                "failed": sum(
                    1
                    for rows in results_by_model.values()
                    for row in rows
                    if row.get("status") != "completed"
                ),
            }
            publish_progress(progress)
            print(
                "library collection triage: "
                f"model={model_name} collection_id={progress['collection_id']} "
                f"gold={example.get('gold_verdict')} verdict={evaluation.get('verdict')} "
                f"confidence={evaluation.get('confidence')} status={evaluation.get('status')} "
                f"reused={was_reused}",
                flush=True,
            )
        if stopped:
            break

    model_summaries = [
        {"model": model_name, **calculate_model_metrics(results_by_model.get(model_name, []))}
        for model_name in models
        if results_by_model.get(model_name)
    ]
    return (
        {
            "kind": "library.collection_triage_benchmark_summary",
            "prompt_version": PROMPT_VERSION,
            "gold_examples": len(examples),
            "models_requested": list(models),
            "processed": processed,
            "reused": reused,
            "stopped": stopped,
            "models": model_summaries,
        },
        report_evaluations,
    )


class PostgresCollectionTriageRepository:
    """PostgreSQL evidence reader and model-evaluation checkpoint store."""

    def __init__(self, engine: Engine, *, schema: str, limit: int | None = None) -> None:
        if not schema.replace("_", "a").isalnum() or schema[0].isdigit():
            raise ValueError("Invalid database schema")
        self.engine = engine
        self.schema = schema
        self.limit = limit

    def _set_search_path(self, conn: Any) -> None:
        conn.execute(text(f'SET search_path TO "{self.schema}", public'))

    def list_gold_examples(self) -> list[dict[str, Any]]:
        limit_sql = " LIMIT :limit" if self.limit is not None else ""
        params = {"limit": max(1, int(self.limit or 1))} if self.limit is not None else {}
        examples: list[dict[str, Any]] = []
        with self.engine.connect() as conn:
            self._set_search_path(conn)
            collections = conn.execute(
                text(
                    """
                    SELECT *
                    FROM library_collections
                    WHERE status IN ('approved', 'rejected')
                    ORDER BY collection_id ASC
                    """
                    + limit_sql
                ),
                params,
            ).mappings().all()
            for collection in collections:
                rows = conn.execute(
                    text(
                        """
                        SELECT i.md5, i.item_title, i.item_hint, i.signal_json,
                               m.lib, m.schema_org
                        FROM library_collection_items i
                        LEFT JOIN metadata m ON m.md5 = i.md5
                        WHERE i.collection_id = :collection_id
                        ORDER BY i.md5 ASC
                        """
                    ),
                    {"collection_id": int(collection["collection_id"])},
                ).mappings().all()
                examples.append(
                    {
                        "collection_id": int(collection["collection_id"]),
                        "gold_verdict": (
                            "approve" if collection["status"] == "approved" else "reject"
                        ),
                        "evidence": _build_collection_review_payload(
                            dict(collection),
                            [dict(row) for row in rows],
                            sample_limit=8,
                            outlier_limit=20,
                        ),
                    }
                )
        return examples

    def find_cached_evaluation(self, **identity: Any) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            self._set_search_path(conn)
            row = conn.execute(
                text(
                    """
                    SELECT *
                    FROM library_collection_ai_evaluations
                    WHERE collection_id = :collection_id
                      AND model_name = :model_name
                      AND prompt_version = :prompt_version
                      AND input_hash = :input_hash
                      AND status = 'completed'
                    """
                ),
                identity,
            ).mappings().first()
        if not row:
            return None
        payload = dict(row)
        payload["signals"] = json.loads(str(payload.pop("signals_json") or "[]"))
        return payload

    def save_evaluation(self, evaluation: dict[str, Any]) -> None:
        payload = dict(evaluation)
        payload["signals_json"] = json.dumps(payload.get("signals") or [], ensure_ascii=False)
        with self.engine.begin() as conn:
            self._set_search_path(conn)
            conn.execute(
                text(
                    """
                    INSERT INTO library_collection_ai_evaluations (
                        collection_id, run_id, model_name, prompt_version, input_hash,
                        gold_verdict, verdict, confidence, rationale, signals_json,
                        raw_response, status, error_text, latency_ms, prompt_tokens,
                        output_tokens, started_at, completed_at, created_at, updated_at
                    ) VALUES (
                        :collection_id, :run_id, :model_name, :prompt_version, :input_hash,
                        :gold_verdict, :verdict, :confidence, :rationale, :signals_json,
                        :raw_response, :status, :error_text, :latency_ms, :prompt_tokens,
                        :output_tokens, :started_at, :completed_at, :completed_at, :completed_at
                    )
                    ON CONFLICT (collection_id, model_name, prompt_version, input_hash)
                    DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        gold_verdict = EXCLUDED.gold_verdict,
                        verdict = EXCLUDED.verdict,
                        confidence = EXCLUDED.confidence,
                        rationale = EXCLUDED.rationale,
                        signals_json = EXCLUDED.signals_json,
                        raw_response = EXCLUDED.raw_response,
                        status = EXCLUDED.status,
                        error_text = EXCLUDED.error_text,
                        latency_ms = EXCLUDED.latency_ms,
                        prompt_tokens = EXCLUDED.prompt_tokens,
                        output_tokens = EXCLUDED.output_tokens,
                        started_at = EXCLUDED.started_at,
                        completed_at = EXCLUDED.completed_at,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                payload,
            )
