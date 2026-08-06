"""CLI entry point for the local collection-triage benchmark."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine


def _bootstrap_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


_bootstrap_repo_root()

from app.db import Database  # noqa: E402
from app.modules.library.collection_triage import (  # noqa: E402
    PostgresCollectionTriageRepository,
    run_collection_triage_benchmark,
)
from app.modules.library.local_llm import OllamaClient, load_local_llm_settings  # noqa: E402
from app.run_artifact_channel import emit_run_artifact  # noqa: E402
from app.settings import load_settings  # noqa: E402


TASK_ID = "library.collection_triage_benchmark"
PANEL_ID = "library"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark local models on collection triage")
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help="Model to evaluate; repeat to override configured models",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit gold examples for a smoke run")
    return parser.parse_args()


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


def _artifacts_root() -> Path:
    configured = str(os.environ.get("MANZARA_ARTIFACTS_ROOT") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".manzara"


def _write_report(run_id: int, summary: dict[str, Any], evaluations: list[dict[str, Any]]) -> Path:
    target = (
        _artifacts_root()
        / "library"
        / "local_llm"
        / "collection-triage"
        / f"run-{run_id}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"summary": summary, "evaluations": evaluations}, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def main() -> int:
    args = _parse_args()
    run_id = _run_id()
    app_settings = load_settings()
    llm_settings = load_local_llm_settings()
    models = tuple(dict.fromkeys(args.models or llm_settings.collection_triage_models))
    if args.limit is not None and int(args.limit) <= 0:
        raise ValueError("--limit must be positive")

    db = Database(app_settings.database_url, schema=app_settings.database_schema)
    engine = create_engine(app_settings.database_url, pool_pre_ping=True)
    repository = PostgresCollectionTriageRepository(
        engine,
        schema=app_settings.database_schema,
        limit=args.limit,
    )
    client = OllamaClient(llm_settings)
    stop_state = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_state["requested"] = True
        print(
            "library collection triage: graceful stop requested; finishing current collection",
            flush=True,
        )

    def publish_progress(progress: dict[str, Any]) -> None:
        db.update_run_progress(run_id, progress)
        db.insert_event(
            "task.progress",
            task_id=TASK_ID,
            run_id=run_id,
            panel_id=PANEL_ID,
            payload={"status": "running", "progress": progress},
        )

    signal.signal(signal.SIGINT, request_stop)
    print(
        f"library collection triage: start run_id={run_id} models={','.join(models)} "
        f"endpoint={llm_settings.endpoint}",
        flush=True,
    )
    try:
        client.preflight(models)
        summary, evaluations = run_collection_triage_benchmark(
            repository=repository,
            client=client,
            models=models,
            run_id=run_id,
            publish_progress=publish_progress,
            should_stop=lambda: bool(stop_state["requested"]),
        )
        report_path = _write_report(run_id, summary, evaluations)
        summary["report_path"] = str(report_path)
        emit_run_artifact(summary)
        print(
            "library collection triage: final "
            + json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
        return 0
    finally:
        client.close()
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
