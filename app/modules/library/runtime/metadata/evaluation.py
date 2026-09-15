"""Metadata evaluation batch orchestration and CLI-facing entry point."""

from __future__ import annotations

import os
import threading
import time
from queue import Queue

from app.db import Database
from app.gemini_config import load_required_gemini_model_pool
from app.gemini_runtime import GeminiRuntimeError, GeminiRuntimeManager
from app.gemini_workers import emit_gemini_worker_log
from app.modules.runtime_shared_utils import read_config
from app.postgres_engine import is_transient_postgres_error
from app.settings import load_settings

from .evaluation_channel import Channel
from .evaluation_progress import PANEL_ID, TASK_ID, _EvaluationProgress
from .evaluation_selection import (
    _count_remaining,
    _early_skip,
    _load_batch,
    _load_known_classifications,
    _save_non_applicable,
)
from .evaluation_types import EvaluationTask
from .evaluation_worker import LibraryApplicabilityWorker

ERROR_BACKOFF_SECONDS = 5


def _run_id() -> int | None:
    raw = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def evaluate(args) -> None:
    """Run batch evaluation and save results into `metadata.lib`."""
    config = read_config()
    settings = load_settings()
    state_db = Database(
        settings.database_url,
        schema=settings.database_schema,
        local_state_path=settings.local_state_path,
    )
    models = load_required_gemini_model_pool()
    run_id = _run_id()
    stop_event = threading.Event()
    gemini_manager = GeminiRuntimeManager(
        state_db,
        task_id=TASK_ID,
        panel_id=PANEL_ID,
        should_stop=stop_event.is_set,
    )
    channel = Channel(dry_run=args.dry_run)
    coordinator_log = lambda message: emit_gemini_worker_log(  # noqa: E731
        message, worker_id="coordinator"
    )
    if args.dry_run:
        coordinator_log(
            "Running in dry-run mode: no DB/file state changes will be persisted."
        )

    remaining = _count_remaining(config, channel, models)
    coordinator_log(f"Documents remaining for evaluation: {remaining}")
    progress = _EvaluationProgress(state_db, run_id=run_id, total=remaining)
    progress.publish()
    excerpt_chars = max(0, args.excerpt_chars)
    while not stop_event.is_set():
        tasks_queue = None
        workers: list[threading.Thread] = []
        try:
            docs = _load_batch(config, args.batch_size, channel, models)
            if not docs:
                coordinator_log("No more documents to process")
                break

            docs, non_applicables = _early_skip(docs)
            if not args.dry_run:
                _save_non_applicable(non_applicables)
                progress.record_completed(
                    "rules_skipped",
                    count=len(non_applicables),
                )
            if not docs:
                continue

            known_classifications = _load_known_classifications()

            tasks_queue = _create_queue(docs)
            worker_count = max(1, min(int(args.workers), tasks_queue.qsize()))
            coordinator_log(
                f"Processing batch of {tasks_queue.qsize()} documents with {worker_count} worker(s)"
            )

            for index in range(worker_count):
                worker = LibraryApplicabilityWorker(
                    tasks_queue=tasks_queue,
                    config=config,
                    channel=channel,
                    dry_run=args.dry_run,
                    excerpt_chars=excerpt_chars,
                    known_classifications=known_classifications,
                    stop_event=stop_event,
                    gemini_manager=gemini_manager,
                    models=models,
                    run_id=run_id,
                    progress=progress,
                )
                thread = threading.Thread(target=worker, name=f"eval-{index + 1}")
                thread.start()
                workers.append(thread)
                time.sleep(2)

            for thread in workers:
                thread.join()

            channel.dump()
            if fatal_error := channel.get_fatal_error():
                raise GeminiRuntimeError(fatal_error)
        except GeminiRuntimeError:
            stop_event.set()
            if tasks_queue is not None:
                tasks_queue.queue.clear()
            for thread in workers:
                thread.join(timeout=120)
            channel.dump()
            raise
        except (KeyboardInterrupt, Exception) as e:  # noqa: BLE001
            is_interrupt = isinstance(e, KeyboardInterrupt)
            if is_interrupt:
                coordinator_log("Interrupted, shutting down workers...")
                stop_event.set()
            else:
                import traceback

                coordinator_log(f"Error during evaluation batch: {e}")
                coordinator_log(traceback.format_exc())

            if tasks_queue is not None:
                tasks_queue.queue.clear()
            for thread in workers:
                thread.join(timeout=120)
            channel.dump()

            if is_interrupt:
                return
            if is_transient_postgres_error(e):
                time.sleep(ERROR_BACKOFF_SECONDS)
            continue


def _create_queue(docs: list[EvaluationTask]) -> Queue:
    tasks_queue: Queue = Queue()
    for doc in docs:
        tasks_queue.put(doc)
    return tasks_queue
