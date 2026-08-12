"""Run resumable Library metadata extraction with ordered Gemini models."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import signal
import sys
from typing import Any, Callable, Mapping


def _bootstrap_repo_root() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


_bootstrap_repo_root()

from boto3 import Session
from botocore.config import Config

from app.db import Database
from app.document_storage import load_document_storage_settings
from app.gemini_config import load_required_gemini_model_pool
from app.gemini_model_pool import (
    GeminiModelPoolExhaustedError,
    GeminiModelPoolOperationalError,
    GeminiModelPoolUnavailableError,
    run_ordered_model_pool,
)
from app.gemini_requests import generate_structured_json
from app.gemini_runtime import GeminiRuntimeManager, GeminiStopRequestedError
from app.modules.library.metadata_extraction import (
    MetadataExtractionRepository,
    parse_metadata_response,
    prepare_metadata_request,
)
from app.modules.library.runtime.metadata.schema import Book
from app.run_artifact_channel import emit_run_artifact
from app.runtime_config import load_runtime_config
from app.settings import load_settings


TASK_ID = "library.metadata_extract"
PANEL_ID = "library"
MODEL_POOL_ALIAS = "library_metadata_extraction"


def _primary_s3_config() -> Config:
    """Keep one unavailable object-store request from stalling the batch."""
    return Config(
        signature_version="s3v4",
        connect_timeout=10,
        read_timeout=30,
        retries={"mode": "standard", "total_max_attempts": 2},
        s3={"addressing_style": "path"},
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract document metadata")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional document limit for a controlled smoke run",
    )
    return parser.parse_args()


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


def _publish_progress(
    db: Database,
    run_id: int,
    *,
    current: int,
    total: int,
    counters: Mapping[str, int],
    model_attempts: Mapping[str, int],
    model_successes: Mapping[str, int],
) -> None:
    resolved = sum(
        int(counters.get(key, 0))
        for key in ("succeeded", "already_complete", "terminal")
    )
    payload = {
        "current": int(current),
        "total": int(total),
        "percent": 100 if total == 0 else round((current / total) * 100, 2),
        "remaining": max(0, int(total) - resolved),
        **{key: int(value) for key, value in counters.items()},
        "model_attempts": dict(model_attempts),
        "model_successes": dict(model_successes),
    }
    db.update_run_progress(run_id, payload)
    db.insert_event(
        "task.progress",
        task_id=TASK_ID,
        run_id=run_id,
        panel_id=PANEL_ID,
        payload={"status": "running", "progress": payload},
    )


def run_metadata_extraction(
    *,
    repository: MetadataExtractionRepository,
    db: Database,
    storage: Any,
    primary_s3: Any,
    models: list[str],
    workspace: Path,
    run_id: int,
    should_stop: Callable[[], bool],
    limit: int | None = None,
    request_json: Callable[..., str] = generate_structured_json,
) -> dict[str, Any]:
    """Process one fixed candidate snapshot and return a structured summary."""
    candidates = repository.list_candidates(limit=limit)
    total = len(candidates)
    counters: Counter[str] = Counter(
        succeeded=0,
        already_complete=0,
        terminal=0,
        source_deferred=0,
        quota_deferred=0,
        service_deferred=0,
    )
    model_attempts: Counter[str] = Counter()
    model_successes: Counter[str] = Counter()
    processed = 0
    outcome = "completed"
    manager = GeminiRuntimeManager(
        db,
        task_id=TASK_ID,
        panel_id=PANEL_ID,
        should_stop=should_stop,
    )
    _publish_progress(
        db,
        run_id,
        current=0,
        total=total,
        counters=counters,
        model_attempts=model_attempts,
        model_successes=model_successes,
    )
    print(
        f"library metadata: start run_id={run_id} eligible={total} "
        f"models={json.dumps(models)}",
        flush=True,
    )

    for candidate in candidates:
        if should_stop():
            outcome = "stopped"
            break
        print(
            f"library metadata: document start md5={candidate.md5} "
            f"source={'content' if candidate.content_url else 'primary_s3'}",
            flush=True,
        )
        request = None
        try:
            print(
                f"library metadata: source prepare start md5={candidate.md5}",
                flush=True,
            )
            request = prepare_metadata_request(
                candidate,
                workspace=workspace,
                storage=storage,
                primary_s3=primary_s3,
            )
            print(
                f"library metadata: source prepare complete md5={candidate.md5}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            counters["source_deferred"] += 1
            processed += 1
            print(
                f"library metadata: source failure md5={candidate.md5} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
            shutil.rmtree(workspace / candidate.md5, ignore_errors=True)
            _publish_progress(
                db,
                run_id,
                current=processed,
                total=total,
                counters=counters,
                model_attempts=model_attempts,
                model_successes=model_successes,
            )
            continue

        def call_model(model_name: str, api_key: str, _lease: Any) -> str:
            model_attempts[model_name] += 1
            print(
                f"library metadata: model attempt md5={candidate.md5} "
                f"model={model_name}",
                flush=True,
            )
            return request_json(
                api_key=api_key,
                model_name=model_name,
                contents=request.contents,
                response_schema=Book,
                files=request.files,
                timeout_seconds=360,
            )

        def record_failure(model_name: str, kind: str, error: str) -> None:
            repository.record_model_failure(
                candidate.md5,
                model_name=model_name,
                kind=kind,
                error=error,
                models=models,
                run_id=run_id,
            )
            print(
                f"library metadata: model failed md5={candidate.md5} "
                f"model={model_name} kind={kind} error={error}",
                flush=True,
            )

        try:
            result = run_ordered_model_pool(
                manager=manager,
                models=models,
                already_attempted=candidate.attempted_models,
                request=call_model,
                parse=parse_metadata_response,
                record_failure=record_failure,
                run_id=run_id,
            )
        except GeminiModelPoolExhaustedError as exc:
            repository.mark_terminal(
                candidate.md5,
                models=models,
                run_id=run_id,
                reason=str(exc),
            )
            counters["terminal"] += 1
            processed += 1
            print(
                f"library metadata: terminal md5={candidate.md5} reason={exc}",
                flush=True,
            )
        except GeminiModelPoolUnavailableError as exc:
            counters["quota_deferred"] += 1
            processed += 1
            globally_exhausted = set(exc.unavailable_models) == set(models)
            if globally_exhausted:
                outcome = "all_keys_exhausted"
            print(
                f"library metadata: quota deferred md5={candidate.md5} "
                f"global={globally_exhausted} reason={exc}",
                flush=True,
            )
        except GeminiModelPoolOperationalError as exc:
            print(
                f"library metadata: operational failure md5={candidate.md5} "
                f"retryable={exc.retryable} error={exc}",
                flush=True,
            )
            if exc.retryable:
                counters["service_deferred"] += 1
                processed += 1
            else:
                outcome = "gemini_unavailable"
        except GeminiStopRequestedError:
            outcome = "stopped"
        else:
            stored = repository.save_success(
                candidate.md5,
                schema_org=result.value,
                model_name=result.model_name,
            )
            counters["succeeded" if stored else "already_complete"] += 1
            model_successes[result.model_name] += int(stored)
            processed += 1
            print(
                f"library metadata: document success md5={candidate.md5} "
                f"model={result.model_name} stored={stored}",
                flush=True,
            )
        finally:
            shutil.rmtree(workspace / candidate.md5, ignore_errors=True)

        _publish_progress(
            db,
            run_id,
            current=processed,
            total=total,
            counters=counters,
            model_attempts=model_attempts,
            model_successes=model_successes,
        )
        if outcome != "completed":
            break
        if should_stop():
            outcome = "stopped"
            break

    resolved = sum(
        int(counters.get(key, 0))
        for key in ("succeeded", "already_complete", "terminal")
    )
    summary = {
        "kind": "library.metadata_extraction_summary",
        "outcome": outcome,
        "eligible": total,
        "processed": processed,
        "remaining": max(0, total - resolved),
        **dict(counters),
        "model_attempts": dict(model_attempts),
        "model_successes": dict(model_successes),
        "stopped": outcome == "stopped",
    }
    print(
        f"library metadata: final {json.dumps(summary, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )
    return summary


def main() -> int:
    args = _parse_args()
    run_id = _run_id()
    app_settings = load_settings()
    config = load_runtime_config()
    models = load_required_gemini_model_pool(MODEL_POOL_ALIAS)
    storage = load_document_storage_settings(config)
    workspace = Path(
        os.environ.get("MANZARA_ARTIFACTS_ROOT", "~/.manzara")
    ).expanduser() / "library" / "metadata-extraction" / f"run-{run_id}"
    workspace.mkdir(parents=True, exist_ok=True)
    repository = MetadataExtractionRepository(
        app_settings.database_url,
        schema=app_settings.database_schema,
    )
    db = Database(app_settings.database_url, schema=app_settings.database_schema)
    primary_s3 = Session().client(
        "s3",
        aws_access_key_id=storage.primary.access_key_id,
        aws_secret_access_key=storage.primary.secret_access_key,
        endpoint_url=storage.primary.endpoint_url,
        region_name=storage.primary.region_name,
        config=_primary_s3_config(),
    )
    primary_s3.head_bucket(Bucket=storage.public_bucket)
    primary_s3.head_bucket(Bucket=storage.private_bucket)
    stop = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop["requested"] = True
        print(
            "library metadata: graceful stop requested; finishing current request",
            flush=True,
        )

    signal.signal(signal.SIGINT, request_stop)
    try:
        summary = run_metadata_extraction(
            repository=repository,
            db=db,
            storage=storage,
            primary_s3=primary_s3,
            models=models,
            workspace=workspace,
            run_id=run_id,
            should_stop=lambda: bool(stop["requested"]),
            limit=args.limit,
        )
        emit_run_artifact(summary)
        return 1 if summary["outcome"] == "gemini_unavailable" else 0
    finally:
        repository.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
