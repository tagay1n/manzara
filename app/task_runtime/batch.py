"""Sequential noninteractive execution using the retained task runtime."""

from __future__ import annotations

from contextlib import ExitStack
import signal
import threading
import time
from typing import Any, Callable

from app.db import Database
from app.runtime_states import TASK_RUN_STATUS_COMPLETED, TASK_RUN_STATUS_STOPPED
from app.task_runtime.contracts import RunOptions, TaskDescriptor
from app.task_runtime.session import SessionLock
from app.tasks import TaskRunner


def _run_stages(db, runner, descriptors, stop, deadline, on_result) -> int:
    for descriptor in descriptors:
        if stop.is_set() or time.monotonic() >= deadline:
            return 130
        started = runner.start_task(descriptor.task_id, options=RunOptions())
        run_id = started["run"]["run_id"]
        print(f"Starting {descriptor.task_id} run_id={run_id}", flush=True)
        stopping = False
        while not runner.is_idle():
            if not stopping and (stop.is_set() or time.monotonic() >= deadline):
                stop.set()
                stopping = True
                print("Safe stop requested; finishing the current operation", flush=True)
                runner.request_shutdown()
            time.sleep(0.5)
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
