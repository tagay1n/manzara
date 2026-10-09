"""Background task execution sharing the application's database engine."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import threading
from typing import Any, Callable

from app.artifacts import task_runs_dir
from app.db import Database
from app.run_log_store import read_run_log
from app.runtime_states import (
    TASK_RUN_STATUS_COMPLETED,
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_STARTING,
    TASK_RUN_STATUS_STOPPED,
    task_terminal_event_type,
)
from app.task_runtime.contracts import RunContext, RunOptions, TaskDescriptor
from app.task_runtime.logging import RunLog, bind_run_log, redact


@dataclass
class _RunHandle:
    context: RunContext
    thread: threading.Thread


class TaskRunner:
    def __init__(self, db: Database, descriptors: list[TaskDescriptor], *,
                 console_sink: Callable[[str], None] | None = None):
        self.db = db
        self.descriptors = {item.task_id: item for item in descriptors}
        self._lock = threading.RLock()
        self._runs: dict[str, _RunHandle] = {}
        self._closing = False
        self._root = task_runs_dir()
        self._console_sink = console_sink

    def start_task(self, task_id: str, *, options: RunOptions) -> dict[str, Any]:
        with self._lock:
            if self._closing:
                raise ValueError("Manzara is shutting down")
            descriptor = self.descriptors[task_id]
            if not descriptor.available:
                raise ValueError(descriptor.unavailable_reason)
            if descriptor.workers_max is not None and options.workers > descriptor.workers_max:
                raise ValueError(f"{descriptor.title} supports at most {descriptor.workers_max} worker(s)")
            handle = self._runs.get(task_id)
            if handle is not None and handle.thread.is_alive():
                return {"action": "noop", "reason": "already_running", "run": self.db.get_run(handle.context.run_id)}
            active = self.db.get_active_run_for_task(task_id)
            if active:
                raise ValueError("A persisted active run needs recovery; reopen Manzara before starting another run")
            run_id = self.db.create_run(task_id=task_id, panel_id=descriptor.group_id,
                                        workers=options.workers)
            try:
                log = RunLog(self._root, task_id, descriptor.group_id, run_id, self._console_sink)
                context = RunContext(
                    db=self.db, task_id=task_id, panel_id=descriptor.group_id, run_id=run_id,
                    options=options, stop_event=threading.Event(), log=log,
                    progress=lambda progress, force=False: self.db.publish_run_progress(
                        run_id=run_id, progress=progress, force=force,
                    ),
                    artifact=lambda payload: self._publish_artifact(task_id, descriptor.group_id, run_id, payload),
                )
                self.db.update_run_summary(run_id, {"options": options.as_dict(), "log_path": str(log.path)})
                self.db.insert_event("task.started", task_id, run_id, descriptor.group_id, {"status": TASK_RUN_STATUS_STARTING})
                thread = threading.Thread(target=self._execute, args=(descriptor, context, log),
                                          name=f"run-{run_id}", daemon=False)
                self._runs[task_id] = _RunHandle(context, thread)
                thread.start()
            except Exception as exc:
                if "log" in locals():
                    log.close()
                self._runs.pop(task_id, None)
                self.db.finish_run(run_id, TASK_RUN_STATUS_FAILED, 1, redact(exc))
                raise
            return {"action": "start", "run": self.db.get_run(run_id)}

    def stop_task(self, task_id: str) -> None:
        with self._lock:
            handle = self._runs.get(task_id)
            if handle is None or not handle.thread.is_alive():
                return
            handle.context.stop_event.set()
            self.db.set_stop_mode(handle.context.run_id, "graceful")
            self.db.insert_event("task.stop_requested", task_id, handle.context.run_id,
                                 handle.context.panel_id, {"mode": "graceful"})

    def request_shutdown(self) -> None:
        with self._lock:
            self._closing = True
            errors = []
            for task_id in list(self._runs):
                try:
                    self.stop_task(task_id)
                except Exception as exc:
                    errors.append(redact(exc))
            if errors:
                raise RuntimeError("Safe stop requested, but recording stop state failed: " + "; ".join(errors))

    def is_idle(self) -> bool:
        with self._lock:
            return not any(handle.thread.is_alive() for handle in self._runs.values())

    def shutdown(self) -> None:
        try:
            self.request_shutdown()
        finally:
            with self._lock:
                threads = [handle.thread for handle in self._runs.values()]
            for thread in threads:
                thread.join()

    def _publish_artifact(self, task_id: str, group_id: str, run_id: int, payload: dict) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("kind"), str) or not payload["kind"]:
            raise ValueError("Structured run artifacts require a kind")
        from app.run_log_store import safe_task_slug

        target = self._root / safe_task_slug(task_id) / f"run-{run_id}.artifact.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
        compact = {key: value for key, value in payload.items()
                   if isinstance(value, (str, int, float, bool)) or value is None}
        compact["artifact_path"] = str(target)
        self.db.insert_event("task.artifact", task_id, run_id, group_id, compact)

    def _execute(self, descriptor: TaskDescriptor, context: RunContext, log: RunLog) -> None:
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(context.run_id, heartbeat_stop),
                                     name=f"heartbeat-{context.run_id}", daemon=True)
        summary: dict[str, Any] = {}
        status = TASK_RUN_STATUS_FAILED
        error = None
        try:
            with bind_run_log(log):
                self.db.mark_run_started(context.run_id, os.getpid())
                if context.should_stop():
                    self.db.set_stop_mode(context.run_id, "graceful")
                heartbeat.start()
                log(f"task start options={json.dumps(context.options.as_dict())}")
                context.progress({"phase": "discovering"}, force=True)
                summary = descriptor.execute(context)
                outcome = summary.get("outcome", "completed")
                status = TASK_RUN_STATUS_FAILED if outcome == "failed" else (
                    TASK_RUN_STATUS_STOPPED if context.should_stop() or outcome == "stopped" else TASK_RUN_STATUS_COMPLETED
                )
        except BaseException as exc:
            error = redact(exc)
            log(f"task failure type={type(exc).__name__} reason={error}", level="ERROR")
            summary = {"kind": "task.failure", "outcome": "failed", "error": error}
        finally:
            heartbeat_stop.set()
            if heartbeat.ident is not None:
                heartbeat.join()
            try:
                persisted = self.db.get_run(context.run_id) or {}
                summary = {**summary, "options": context.options.as_dict(), "log_path": str(log.path),
                           "progress": persisted.get("progress", {})}
                if not summary.get("kind"):
                    summary["kind"] = "task.summary"
                context.artifact(summary)
                self.db.update_run_summary(context.run_id, summary)
                self.db.finish_run(context.run_id, status, 1 if status == TASK_RUN_STATUS_FAILED else 0, error)
                self.db.insert_event(task_terminal_event_type(status), context.task_id, context.run_id,
                                     context.panel_id, {"status": status, "exit_code": 1 if status == TASK_RUN_STATUS_FAILED else 0,
                                                        **({"error": error} if error else {})})
                log(f"task final status={status} summary={json.dumps(summary, ensure_ascii=False)}")
            except Exception as exc:
                log(f"task finalization failed type={type(exc).__name__} reason={redact(exc)}", level="ERROR")
                # Do not hide a failed artifact/checkpoint finalization behind a success state.
                try:
                    self.db.finish_run(context.run_id, TASK_RUN_STATUS_FAILED, 1, redact(exc))
                except Exception as final_error:
                    log(f"run remains recoverable after persistence failure: {redact(final_error)}", level="ERROR")
            finally:
                log.close()

    def _heartbeat(self, run_id: int, stop: threading.Event) -> None:
        while not stop.wait(5):
            try:
                self.db.heartbeat(run_id)
            except Exception:
                return

    def get_run_logs(self, *, task_id: str, run_id: int, **options) -> list[dict]:
        return read_run_log(self._root, task_id, run_id, **options)
