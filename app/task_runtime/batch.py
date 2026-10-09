"""Sequential noninteractive execution using the retained task runtime."""

from __future__ import annotations

from contextlib import ExitStack
import json
import signal
import threading
import time
from typing import Any, Callable

from app.db import Database
from app.runtime_states import TASK_RUN_STATUS_COMPLETED, TASK_RUN_STATUS_STOPPED
from app.task_runtime.contracts import RunOptions, TaskDescriptor
from app.task_runtime.logging import redact
from app.task_runtime.session import SessionLock
from app.tasks import TaskRunner


_LOG_PAGE_SIZE = 200
_STATUS_INTERVAL_SECONDS = 30


def _stream_logs(runner, task_id, run_id, cursor) -> tuple[int, int]:
    rows = runner.get_run_logs(task_id=task_id, run_id=run_id,
                               after_log_id=cursor, limit=_LOG_PAGE_SIZE)
    for row in rows:
        print(f"{row['ts']} | task_id={task_id} run_id={run_id} | {row['line']}", flush=True)
    return (rows[-1]["log_id"] if rows else cursor), len(rows)


def _print_status(db, run_id, started_at, last_log_output) -> None:
    run = db.get_run(run_id)
    if run is None:
        raise RuntimeError(f"Run {run_id} is missing during execution")
    now = time.monotonic()
    snapshot = {
        "kind": "task.status", "task_id": run["task_id"], "run_id": run_id,
        "status": run["status"], "elapsed_seconds": int(now - started_at),
        "seconds_since_log_output": int(now - last_log_output),
        "progress": run.get("progress") or {},
    }
    print(redact(json.dumps(snapshot, ensure_ascii=True)), flush=True)


def _run_stages(db, runner, descriptors, stop, deadline, on_result) -> int:
    for descriptor in descriptors:
        if stop.is_set() or time.monotonic() >= deadline:
            return 130
        started = runner.start_task(descriptor.task_id, options=RunOptions())
        run_id = started["run"]["run_id"]
        print(f"Starting {descriptor.task_id} run_id={run_id}", flush=True)
        started_at = last_log_output = time.monotonic()
        next_status = started_at + _STATUS_INTERVAL_SECONDS
        cursor = 0
        stopping = False
        while not runner.is_idle():
            cursor, count = _stream_logs(runner, descriptor.task_id, run_id, cursor)
            now = time.monotonic()
            if count:
                last_log_output = now
            if now >= next_status:
                _print_status(db, run_id, started_at, last_log_output)
                next_status = now + _STATUS_INTERVAL_SECONDS
            if not stopping and (stop.is_set() or time.monotonic() >= deadline):
                stop.set()
                stopping = True
                print("Safe stop requested; finishing the current operation", flush=True)
                runner.request_shutdown()
            time.sleep(0.5)
        # Drain the final lines in bounded pages, including worker finalization.
        while True:
            cursor, count = _stream_logs(runner, descriptor.task_id, run_id, cursor)
            if count < _LOG_PAGE_SIZE:
                break
        # Read after the worker exits, including artifact/event finalization.
        run = db.get_run(run_id)
        if run is None:
            raise RuntimeError(f"Run {run_id} is missing after execution")
        on_result(run)
        if run["status"] != TASK_RUN_STATUS_COMPLETED or run.get("exit_code") != 0:
            return 130 if stop.is_set() or run["status"] == TASK_RUN_STATUS_STOPPED else 1
    return 130 if stop.is_set() else 0


def run_batch(
    settings: Any,
    descriptors: list[TaskDescriptor],
    *,
    on_result: Callable[[dict[str, Any]], None],
    preflight: Callable[[Database], None],
    budget_seconds: int = 5 * 60 * 60,
) -> int:
    """Own local state and stop cooperatively between sequential task stages."""
    stop = threading.Event()
    deadline = time.monotonic() + budget_seconds
    with ExitStack() as resources:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.signal(signum, lambda _signum, _frame: stop.set())
            resources.callback(signal.signal, signum, previous)
        session = SessionLock(settings.local_state_path)
        session.acquire()
        resources.callback(session.close)
        db = Database(settings.database_url, schema=settings.database_schema,
                      pool_size=settings.database_pool_size, local_state_path=settings.local_state_path)
        resources.callback(db.close)
        db.init_local_state()
        panel_ids = dict.fromkeys(item.definition["panel_id"] for item in descriptors)
        db.seed_panels([{"panel_id": name, "title": name.title()} for name in panel_ids])
        db.seed_tasks([item.definition for item in descriptors])
        recovered = db.recover_active_runs()
        db.recover_active_conveyor_runs()
        if recovered:
            db.insert_event("system.recovery", None, None, None, {"recovered_runs": recovered})
        runner = TaskRunner(db, descriptors)
        resources.callback(runner.shutdown)
        if stop.is_set() or time.monotonic() >= deadline:
            return 130
        preflight(db)
        return _run_stages(db, runner, descriptors, stop, deadline, on_result)
