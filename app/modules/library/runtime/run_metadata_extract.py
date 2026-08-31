"""Run resumable Library metadata extraction with ordered Gemini models."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
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
from app.document_storage import load_document_storage_settings, prune_document_cache
from app.gemini_config import load_required_gemini_model_pool
from app.gemini_model_pool import (
    GeminiModelPoolExhaustedError,
    GeminiModelPoolOperationalError,
    GeminiModelPoolUnavailableError,
    run_ordered_model_pool,
)
from app.gemini_requests import generate_structured_json
from app.gemini_runtime import GeminiRuntimeManager, GeminiStopRequestedError
from app.gemini_workers import resolve_gemini_workers
from app.modules.library.corrupt_document import (
    CorruptDocumentError,
    build_corrupt_cleanup_plan,
)
from app.modules.library.document_cleanup_repository import DocumentCleanupRepository
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
PANEL_ID = "metadata"
DEFAULT_OPERATIONAL_RETRY_COOLDOWN_SECONDS = 21_600
PROGRESS_COUNTER_KEYS = (
    "succeeded",
    "already_complete",
    "terminal",
    "source_deferred",
    "corrupted_planned",
    "corrupted_plan_reused",
    "quota_deferred",
    "service_deferred",
)


def _operational_retry_cooldown_seconds(config: Mapping[str, Any]) -> int:
    gemini = config.get("gemini", {})
    if not isinstance(gemini, Mapping):
        raise ValueError("gemini config must be an object")
    value = gemini.get(
        "metadata_extraction_operational_retry_cooldown_seconds",
        DEFAULT_OPERATIONAL_RETRY_COOLDOWN_SECONDS,
    )
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            "gemini.metadata_extraction_operational_retry_cooldown_seconds "
            "must be integral"
        )
    if not 60 <= value <= 604_800:
        raise ValueError(
            "gemini.metadata_extraction_operational_retry_cooldown_seconds "
            "must be between 60 and 604800"
        )
    return value


def _primary_s3_config() -> Config:
    """Keep one unavailable object-store request from stalling the batch."""
    return Config(
        signature_version="s3v4",
        connect_timeout=10,
        read_timeout=30,
        retries={"mode": "standard", "total_max_attempts": 2},
        s3={"addressing_style": "path"},
    )


def _format_response_for_log(response_text: str | None) -> str:
    """Pretty-print JSON responses for readable extraction logs."""
    if response_text is None:
        return ""
    raw = response_text.strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract document metadata")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional document limit for a controlled smoke run",
    )
    parser.add_argument("--workers", type=int, default=None)
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


class _AggregateProgressPublisher:
    """Publish one monotonic run snapshot assembled from worker-local state."""

    def __init__(self, db: Database, run_id: int, *, total: int) -> None:
        self._db = db
        self._run_id = int(run_id)
        self._total = int(total)
        self._lock = threading.Lock()
        self._states: dict[
            int,
            tuple[int, Counter[str], Counter[str], Counter[str]],
        ] = {}

    def publish(
        self,
        worker_id: int,
        *,
        current: int,
        counters: Mapping[str, int],
        model_attempts: Mapping[str, int],
        model_successes: Mapping[str, int],
    ) -> None:
        with self._lock:
            self._states[int(worker_id)] = (
                int(current),
                Counter(counters),
                Counter(model_attempts),
                Counter(model_successes),
            )
            aggregate_counters: Counter[str] = Counter()
            aggregate_attempts: Counter[str] = Counter()
            aggregate_successes: Counter[str] = Counter()
            aggregate_current = 0
            for (
                worker_current,
                worker_counters,
                worker_attempts,
                worker_successes,
            ) in self._states.values():
                aggregate_current += worker_current
                aggregate_counters.update(worker_counters)
                aggregate_attempts.update(worker_attempts)
                aggregate_successes.update(worker_successes)
            _publish_progress(
                self._db,
                self._run_id,
                current=aggregate_current,
                total=self._total,
                counters=aggregate_counters,
                model_attempts=aggregate_attempts,
                model_successes=aggregate_successes,
            )


def run_metadata_extraction(
    *,
    repository: MetadataExtractionRepository,
    cleanup_repository: Any | None = None,
    db: Database,
    storage: Any,
    primary_s3: Any,
    models: list[str],
    workspace: Path,
    run_id: int,
    should_stop: Callable[[], bool],
    limit: int | None = None,
    operational_retry_cooldown_seconds: int = (
        DEFAULT_OPERATIONAL_RETRY_COOLDOWN_SECONDS
    ),
    request_json: Callable[..., str] = generate_structured_json,
    workers: int = 1,
    _candidates: list[Any] | None = None,
    _aggregate_progress: _AggregateProgressPublisher | None = None,
    _worker_id: int = 0,
) -> dict[str, Any]:
    """Process one fixed candidate snapshot and return a structured summary."""
    candidates = (
        list(_candidates)
        if _candidates is not None
        else repository.list_candidates(limit=limit)
    )
    worker_count = max(1, min(int(workers), len(candidates) or 1))
    if worker_count > 1 and _candidates is None:
        partitions = [candidates[index::worker_count] for index in range(worker_count)]
        aggregate_progress = _AggregateProgressPublisher(
            db,
            run_id,
            total=len(candidates),
        )
        _publish_progress(
            db,
            run_id,
            current=0,
            total=len(candidates),
            counters={key: 0 for key in PROGRESS_COUNTER_KEYS},
            model_attempts={},
            model_successes={},
        )
        print(
            f"library metadata: worker pool requested={workers} started={worker_count}",
            flush=True,
        )
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="metadata-worker"
        ) as executor:
            results = list(
                executor.map(
                    lambda indexed_partition: run_metadata_extraction(
                        repository=repository,
                        cleanup_repository=cleanup_repository,
                        db=db,
                        storage=storage,
                        primary_s3=primary_s3,
                        models=models,
                        workspace=workspace,
                        run_id=run_id,
                        should_stop=should_stop,
                        limit=None,
                        operational_retry_cooldown_seconds=operational_retry_cooldown_seconds,
                        request_json=request_json,
                        workers=1,
                        _candidates=indexed_partition[1],
                        _aggregate_progress=aggregate_progress,
                        _worker_id=indexed_partition[0],
                    ),
                    enumerate(partitions),
                )
            )
        model_attempts = Counter()
        model_successes = Counter()
        for result in results:
            model_attempts.update(result.get("model_attempts") or {})
            model_successes.update(result.get("model_successes") or {})
        outcomes = [str(result.get("outcome") or "completed") for result in results]
        outcome = next((item for item in outcomes if item != "completed"), "completed")
        summary = {
            "kind": "library.metadata_extraction_summary",
            "outcome": outcome,
            "eligible": len(candidates),
            "processed": sum(int(result.get("processed") or 0) for result in results),
            "remaining": sum(int(result.get("remaining") or 0) for result in results),
            **{
                key: sum(int(result.get(key) or 0) for result in results)
                for key in PROGRESS_COUNTER_KEYS
            },
            "model_attempts": dict(model_attempts),
            "model_successes": dict(model_successes),
            "stopped": outcome == "stopped",
            "workers": worker_count,
        }
        _publish_progress(
            db, run_id, current=summary["processed"], total=len(candidates),
            counters=Counter({key: summary[key] for key in PROGRESS_COUNTER_KEYS}),
            model_attempts=model_attempts, model_successes=model_successes,
        )
        return summary
    total = len(candidates)
    counters: Counter[str] = Counter(
        succeeded=0,
        already_complete=0,
        terminal=0,
        source_deferred=0,
        corrupted_planned=0,
        corrupted_plan_reused=0,
        quota_deferred=0,
        service_deferred=0,
    )
    model_attempts: Counter[str] = Counter()
    model_successes: Counter[str] = Counter()
    processed = 0
    outcome = "completed"

    def publish_progress() -> None:
        if _aggregate_progress is not None:
            _aggregate_progress.publish(
                _worker_id,
                current=processed,
                counters=counters,
                model_attempts=model_attempts,
                model_successes=model_successes,
            )
            return
        _publish_progress(
            db,
            run_id,
            current=processed,
            total=total,
            counters=counters,
            model_attempts=model_attempts,
            model_successes=model_successes,
        )

    manager = GeminiRuntimeManager(
        db,
        task_id=TASK_ID,
        panel_id=PANEL_ID,
        should_stop=should_stop,
    )
    publish_progress()
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
        except CorruptDocumentError as exc:
            if cleanup_repository is None:
                raise RuntimeError(
                    "Corrupt document planning requires a cleanup repository"
                ) from exc
            cleanup_id, created = cleanup_repository.enqueue_cleanup(
                build_corrupt_cleanup_plan(
                    storage=storage,
                    md5=candidate.md5,
                    source_path=candidate.source_path,
                    mime_type=candidate.mime_type,
                    source_size=candidate.primary_storage_size,
                    task_id=TASK_ID,
                    run_id=run_id,
                    error=exc,
                )
            )
            counters[
                "corrupted_planned" if created else "corrupted_plan_reused"
            ] += 1
            processed += 1
            print(
                f"library metadata: corrupted plan md5={candidate.md5} "
                f"cleanup_id={cleanup_id} detector={exc.detector} created={created}",
                flush=True,
            )
            shutil.rmtree(workspace / candidate.md5, ignore_errors=True)
            publish_progress()
            continue
        except Exception as exc:  # noqa: BLE001
            counters["source_deferred"] += 1
            processed += 1
            print(
                f"library metadata: source failure md5={candidate.md5} "
                f"error={type(exc).__name__}: {exc}",
                flush=True,
            )
            shutil.rmtree(workspace / candidate.md5, ignore_errors=True)
            publish_progress()
            continue

        def call_model(model_name: str, api_key: str, _lease: Any) -> str:
            model_attempts[model_name] += 1
            print(
                f"library metadata: model attempt md5={candidate.md5} "
                f"model={model_name}",
                flush=True,
            )
            raw_response = request_json(
                api_key=api_key,
                model_name=model_name,
                contents=request.contents,
                response_schema=Book,
                files=request.files,
                timeout_seconds=360,
            )
            print(
                f"library metadata: Gemini response md5={candidate.md5} "
                f"model={model_name}:\n{_format_response_for_log(raw_response)}",
                flush=True,
            )
            return raw_response

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
                repository.record_operational_deferral(
                    candidate.md5,
                    models=models,
                    run_id=run_id,
                    error=str(exc),
                    retry_after_seconds=operational_retry_cooldown_seconds,
                )
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

        publish_progress()
        if outcome != "completed":
            break
        if should_stop():
            outcome = "stopped"
            break

    resolved = sum(
        int(counters.get(key, 0))
        for key in (
            "succeeded",
            "already_complete",
            "terminal",
            "corrupted_planned",
            "corrupted_plan_reused",
        )
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
        "workers": int(workers),
    }
    print(
        f"library metadata: final {json.dumps(summary, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )
    return summary


def main() -> int:
    args = _parse_args()
    workers = resolve_gemini_workers(args.workers)
    run_id = _run_id()
    app_settings = load_settings()
    config = load_runtime_config()
    models = load_required_gemini_model_pool()
    storage = load_document_storage_settings(config)
    prune_document_cache(
        storage.cache_path,
        max_bytes=storage.cache_max_bytes,
    )
    workspace = Path(
        os.environ.get("MANZARA_ARTIFACTS_ROOT", "~/.manzara")
    ).expanduser() / "library" / "metadata-extraction" / f"run-{run_id}"
    workspace.mkdir(parents=True, exist_ok=True)
    repository = MetadataExtractionRepository(
        app_settings.database_url,
        schema=app_settings.database_schema,
    )
    cleanup_repository = DocumentCleanupRepository(
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
            cleanup_repository=cleanup_repository,
            db=db,
            storage=storage,
            primary_s3=primary_s3,
            models=models,
            workspace=workspace,
            run_id=run_id,
            should_stop=lambda: bool(stop["requested"]),
            limit=args.limit,
            workers=workers,
            operational_retry_cooldown_seconds=(
                _operational_retry_cooldown_seconds(config)
            ),
        )
        emit_run_artifact(summary)
        return 1 if summary["outcome"] == "gemini_unavailable" else 0
    finally:
        cleanup_repository.dispose()
        repository.dispose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
