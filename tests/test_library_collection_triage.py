from __future__ import annotations

import json
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.modules.library.collection_triage import (
    PROMPT_VERSION,
    build_triage_prompt,
    calculate_model_metrics,
    parse_triage_response,
    run_collection_triage_benchmark,
)


def _example(collection_id: int, gold_verdict: str) -> dict:
    return {
        "collection_id": collection_id,
        "gold_verdict": gold_verdict,
        "evidence": {
            "collection": {
                "collection_id": collection_id,
                "title": "Ватаным Татарстан",
                "normalized_title": "ватаным татарстан",
                "status": gold_verdict,
                "item_count": 4,
            },
            "summary": {"item_count": 4, "date_coverage": {"percent": 100}},
            "grouping_evidence": [{"key": "shared_parent", "value": "/papers"}],
            "consistency": {"title": {"percent": 100, "dominant": "Ватаным Татарстан"}},
            "outliers": [],
            "samples": [{"title": "Ватаным Татарстан № 1", "file_name": "01.pdf"}],
        },
    }


def test_triage_prompt_excludes_human_verdict() -> None:
    prompt = build_triage_prompt(_example(1, "approved"))

    assert "Ватаным Татарстан" in prompt
    assert '"status"' not in prompt
    assert '"gold_verdict"' not in prompt
    assert "approved" not in prompt.lower()


def test_parse_triage_response_validates_contract() -> None:
    parsed = parse_triage_response(
        json.dumps(
            {
                "verdict": "approve",
                "confidence": 0.91,
                "rationale": "The issue numbering and title are stable.",
                "signals": ["shared title", "issue sequence"],
            }
        )
    )

    assert parsed == {
        "verdict": "approve",
        "confidence": 0.91,
        "rationale": "The issue numbering and title are stable.",
        "signals": ["shared title", "issue sequence"],
    }


def test_model_metrics_include_abstention_and_parse_failures() -> None:
    metrics = calculate_model_metrics(
        [
            {"gold_verdict": "approve", "verdict": "approve", "status": "completed", "latency_ms": 100},
            {"gold_verdict": "approve", "verdict": "uncertain", "status": "completed", "latency_ms": 200},
            {"gold_verdict": "reject", "verdict": "reject", "status": "completed", "latency_ms": 300},
            {"gold_verdict": "reject", "verdict": None, "status": "parse_failed", "latency_ms": 400},
        ]
    )

    assert metrics["total"] == 4
    assert metrics["coverage"] == 50.0
    assert metrics["structured_success"] == 75.0
    assert metrics["confusion"] == {
        "approve": {"approve": 1, "reject": 0, "uncertain": 1, "parse_failed": 0},
        "reject": {"approve": 0, "reject": 1, "uncertain": 0, "parse_failed": 1},
    }
    assert metrics["latency_ms"]["average"] == 250
    assert metrics["latency_ms"]["p95"] == 400


class _FakeRepository:
    def __init__(self) -> None:
        self.examples = [_example(1, "approve"), _example(2, "reject")]
        self.saved: list[dict] = []
        self.cached: dict[tuple, dict] = {}

    def list_gold_examples(self) -> list[dict]:
        return self.examples

    def find_cached_evaluation(self, **identity) -> dict | None:
        return self.cached.get(tuple(identity.values()))

    def save_evaluation(self, evaluation: dict) -> None:
        self.saved.append(evaluation)
        identity = (
            evaluation["collection_id"],
            evaluation["model_name"],
            evaluation["prompt_version"],
            evaluation["input_hash"],
        )
        self.cached[identity] = dict(evaluation)


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def evaluate(self, *, model_name: str, prompt: str) -> dict:
        self.calls.append((model_name, prompt))
        verdict = "approve" if "№ 1" in prompt else "reject"
        return {
            "content": json.dumps(
                {
                    "verdict": verdict,
                    "confidence": 0.8,
                    "rationale": "Evidence is consistent.",
                    "signals": ["consistent evidence"],
                }
            ),
            "latency_ms": 25,
        }


def test_benchmark_reuses_cached_evaluations() -> None:
    repository = _FakeRepository()
    client = _FakeClient()

    first, first_rows = run_collection_triage_benchmark(
        repository=repository,
        client=client,
        models=("qwen3:4b",),
        run_id=42,
        publish_progress=lambda _payload: None,
        should_stop=lambda: False,
    )
    second, second_rows = run_collection_triage_benchmark(
        repository=repository,
        client=client,
        models=("qwen3:4b",),
        run_id=43,
        publish_progress=lambda _payload: None,
        should_stop=lambda: False,
    )

    assert first["kind"] == "library.collection_triage_benchmark_summary"
    assert first["prompt_version"] == PROMPT_VERSION
    assert first["processed"] == 2
    assert first["reused"] == 0
    assert second["processed"] == 2
    assert second["reused"] == 2
    assert len(client.calls) == 2
    assert len(repository.saved) == 2
    assert all(row["reused"] is False for row in first_rows)
    assert all(row["reused"] is True for row in second_rows)


def test_benchmark_stops_before_starting_next_collection() -> None:
    repository = _FakeRepository()
    client = _FakeClient()
    stop = {"requested": False}

    def publish(payload: dict) -> None:
        if payload.get("current") == 1:
            stop["requested"] = True

    summary, _evaluations = run_collection_triage_benchmark(
        repository=repository,
        client=client,
        models=("qwen3:4b",),
        run_id=44,
        publish_progress=publish,
        should_stop=lambda: stop["requested"],
    )

    assert summary["stopped"] is True
    assert summary["processed"] == 1
    assert len(client.calls) == 1


def test_benchmark_retries_one_malformed_response() -> None:
    repository = _FakeRepository()

    class RepairingClient:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate(self, *, model_name: str, prompt: str) -> dict:
            self.calls += 1
            if self.calls == 1:
                return {"content": "not-json", "latency_ms": 10}
            return {
                "content": json.dumps(
                    {
                        "verdict": "approve",
                        "confidence": 0.7,
                        "rationale": "The corrected response follows the contract.",
                        "signals": [],
                    }
                ),
                "latency_ms": 15,
            }

    client = RepairingClient()
    repository.examples = repository.examples[:1]

    summary, _evaluations = run_collection_triage_benchmark(
        repository=repository,
        client=client,
        models=("qwen3:4b",),
        run_id=45,
        publish_progress=lambda _payload: None,
        should_stop=lambda: False,
    )

    assert client.calls == 2
    assert summary["models"][0]["structured_success"] == 100.0
    assert repository.saved[0]["latency_ms"] == 25


def test_collection_triage_migration_has_cache_identity(prepared_test_schema) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_collection_triage_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    try:
        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert inspector.has_table("library_collection_ai_evaluations", schema=schema)
        unique_constraints = inspector.get_unique_constraints(
            "library_collection_ai_evaluations",
            schema=schema,
        )
        assert any(
            constraint["column_names"]
            == ["collection_id", "model_name", "prompt_version", "input_hash"]
            for constraint in unique_constraints
        )
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
