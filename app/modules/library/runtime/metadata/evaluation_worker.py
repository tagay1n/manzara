"""Evaluation worker lifecycle and per-document outcome handling."""

from __future__ import annotations

import threading
from queue import Empty, Queue
from typing import Any

from app.gemini_model_pool import (
    GeminiModelPoolExhaustedError,
    GeminiModelPoolItemRejectedError,
    GeminiModelPoolOperationalError,
    GeminiModelPoolUnavailableError,
)
from app.gemini_runtime import GeminiRuntimeManager, GeminiStopRequestedError
from app.gemini_workers import emit_gemini_worker_log
from app.modules.library.runtime.metadata.repository import mark_evaluation_terminal
from app.postgres_engine import is_transient_postgres_error

from .evaluation_channel import Channel
from .evaluation_documents import EvaluationDocuments
from .evaluation_persistence import save_evaluation_result
from .evaluation_progress import _EvaluationProgress
from .evaluation_request import evaluate_document


class LibraryApplicabilityWorker:
    """Consume document tasks and coordinate outcomes at safe worker boundaries."""

    def __init__(
        self,
        tasks_queue: Queue,
        config: dict,
        channel: "Channel",
        dry_run: bool,
        excerpt_chars: int,
        known_classifications: list[dict[str, Any]] | None = None,
        stop_event: threading.Event | None = None,
        gemini_manager: GeminiRuntimeManager | None = None,
        models: list[str] | None = None,
        run_id: int | None = None,
        progress: _EvaluationProgress | None = None,
    ):
        self.tasks_queue = tasks_queue
        self.config = config
        self.channel = channel
        self.dry_run = dry_run
        self.excerpt_chars = excerpt_chars
        self.known_classifications = known_classifications or []
        self.stop_event = stop_event or threading.Event()
        if gemini_manager is None:
            raise ValueError("gemini_manager is required")
        self.gemini_manager = gemini_manager
        self.models = [str(model) for model in (models or []) if str(model).strip()]
        if not self.models:
            raise ValueError("metadata evaluation models are required")
        self.run_id = run_id
        self.progress = progress
        self.documents = EvaluationDocuments(
            config=config, excerpt_chars=excerpt_chars, log=self.log
        )

    def __call__(self) -> None:
        while True:
            if self.stop_event.is_set():
                self.log("Stop signal received, shutting down")
                return
            try:
                doc = self.tasks_queue.get(block=False)
            except Empty:
                self.log("No tasks left, shutting down")
                return

            try:
                self.log(f"Evaluating {doc.md5}")
                result = evaluate_document(
                    doc,
                    config=self.config,
                    documents=self.documents,
                    manager=self.gemini_manager,
                    models=self.models,
                    known_classifications=self.known_classifications,
                    dry_run=self.dry_run,
                    run_id=self.run_id,
                    progress=self.progress,
                    log=self.log,
                )
                save_evaluation_result(
                    doc.md5,
                    result.value,
                    model_name=result.model_name,
                    dry_run=self.dry_run,
                    log=self.log,
                )
                if self.progress is not None and not self.dry_run:
                    self.progress.record_completed(
                        "succeeded",
                        model_name=result.model_name,
                    )
            except GeminiModelPoolExhaustedError as exc:
                self.log(f"All evaluation models rejected {doc.md5}: {exc}")
                if not self.dry_run:
                    mark_evaluation_terminal(
                        doc.md5,
                        models=self.models,
                        run_id=self.run_id,
                        reason=str(exc),
                    )
                    if self.progress is not None:
                        self.progress.record_completed("terminal")
                continue
            except GeminiModelPoolUnavailableError as exc:
                globally_exhausted = set(exc.unavailable_models) == set(self.models)
                self.log(
                    f"Gemini quota deferred md5={doc.md5} "
                    f"global={globally_exhausted} reason={exc}"
                )
                self.channel.defer_document(doc.md5)
                if self.progress is not None and not self.dry_run:
                    self.progress.record_completed("quota_deferred")
                if globally_exhausted:
                    self.stop_event.set()
                    return
                continue
            except GeminiModelPoolItemRejectedError as exc:
                self.log(
                    f"Gemini rejected evaluation item md5={doc.md5} error={exc}"
                )
                if not self.dry_run:
                    mark_evaluation_terminal(
                        doc.md5,
                        models=self.models,
                        run_id=self.run_id,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                    if self.progress is not None:
                        self.progress.record_completed("terminal")
                continue
            except GeminiModelPoolOperationalError as exc:
                self.log(
                    f"Gemini evaluation operational failure md5={doc.md5} "
                    f"retryable={exc.retryable} error={exc}"
                )
                if exc.retryable:
                    self.channel.defer_document(doc.md5)
                    if self.progress is not None and not self.dry_run:
                        self.progress.record_completed("service_deferred")
                    continue
                self.channel.set_fatal_error(str(exc))
                self.stop_event.set()
                self.tasks_queue.put(doc)
                return
            except GeminiStopRequestedError:
                self.log(f"Stop requested while evaluating md5={doc.md5}")
                self.stop_event.set()
                self.tasks_queue.put(doc)
                return
            except Exception as e:  # noqa: BLE001
                import traceback

                self.log(
                    f"Unhandled error for {doc.md5}: {e}\n{traceback.format_exc()}"
                )
                if is_transient_postgres_error(e):
                    self.log(
                        f"PostgreSQL unavailable; deferring evaluation md5={doc.md5}"
                    )
                    self.channel.defer_document(doc.md5)
                    continue
                if not self.dry_run:
                    mark_evaluation_terminal(
                        doc.md5,
                        models=self.models,
                        run_id=self.run_id,
                        reason=f"{type(e).__name__}: {e}",
                    )
                    if self.progress is not None:
                        self.progress.record_completed("terminal")

    def log(self, message: str) -> None:
        emit_gemini_worker_log(
            message,
            worker_id=threading.current_thread().name,
        )
