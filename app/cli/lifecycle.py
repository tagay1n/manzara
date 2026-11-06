"""Foreground ownership through worker finalization and output drain.

Terminal supplies I/O callbacks; this controller never owns rendering or input.
"""

import asyncio
import time

from app.cli.presentation import duration, elapsed, restrictions, summary_text
from app.runtime_states import TASK_RUN_ACTIVE_STATUSES, TASK_RUN_STATUS_STOPPED
from app.task_runtime.logging import redact


class ForegroundLifecycle:
    def __init__(self, *, get_runner, get_db, is_closing, output_failure,
                 say, spawn, drain, signal_completion, emergency_notice, clear_message):
        self.get_runner = get_runner
        self.get_db = get_db
        self.is_closing = is_closing
        self.output_failure = output_failure
        self._say = say
        self._spawn = spawn
        self._drain = drain
        self._signal_completion = signal_completion
        self._show_emergency_notice = emergency_notice
        self.clear_message = clear_message
        self.stopping = False
        self.starting = False
        self.finalizing = False
        self.foreground = None
        self.foreground_run_id = None
        self.foreground_snapshot = None
        self.foreground_started = 0.0
        self.runtime_error = None
        self._stop_pending = False

    async def start(self, task, options):
        # Claim the slot before any await, including printing or start_task I/O.
        self.foreground = task
        self.starting = True
        self.finalizing = False
        self.stopping = False
        self.foreground_run_id = None
        self.foreground_snapshot = None
        self.foreground_started = time.monotonic()
        self.runtime_error = None
        try:
            if task.requires_full_inventory and options.limit is not None:
                raise ValueError(f"{task.title} requires the complete inventory; clear the limit in /settings")
            text = f"Starting · {task.title} · limit {options.limit or 'unlimited'}"
            if task.task_id == "library.extract_non_pdf":
                text += "\n" + restrictions(options.as_dict())
            await self._say(text)
            if self.stopping or self.is_closing():
                await self._say(f"Stopped · {task.title} · start cancelled before execution.")
                await self._drain()
                await self._signal_completion({"status": TASK_RUN_STATUS_STOPPED})
                self._release_foreground()
                return
            result = await asyncio.to_thread(self.get_runner().start_task, task.task_id, options=options)
            self.foreground_run_id = result["run"]["run_id"]
            self.foreground_snapshot = result["run"]
            if self.stopping or self.is_closing():
                await asyncio.to_thread(self.get_runner().stop_task, task.task_id, run_id=self.foreground_run_id)
        except Exception as exc:
            # start_task returns its run identity before doing any further I/O.
            # If stop persistence fails after that, retain the foreground slot.
            await self._say(f"Start/stop failed: {redact(exc)}")
            if self.foreground_run_id is None:
                await self._drain()
                await self._signal_completion(None, failure=redact(exc) or type(exc).__name__)
                self._release_foreground()
        finally:
            self.starting = False

    def request_stop(self):
        if self.foreground is None:
            self._spawn(self._say("No foreground task is active."))
            return
        if self.stopping:
            return
        self.stopping = True
        self._spawn(self._say("Safe stop requested; active operations finish at checkpoint boundaries. Ctrl-C again forces exit."))
        # During a pending start, start delivers the stop once its ID arrives.
        # Capture existing IDs now so a delayed stop cannot target a later run.
        if self.foreground_run_id is not None:
            self._spawn(self._deliver_stop(self.foreground.task_id, self.foreground_run_id))

    async def _deliver_stop(self, task_id, run_id):
        self._stop_pending = True
        try:
            if self.get_runner():
                await asyncio.to_thread(self.get_runner().stop_task, task_id, run_id=run_id)
        except Exception as exc:
            await self._say("Safe stop recording failed: " + redact(exc) + ". Ctrl-C again forces exit.")
        finally:
            self._stop_pending = False

    def snapshot(self, run_id):
        # Determine worker exit *before* reading the final saved result. A
        # terminal row read before exit can still become a finalization failure.
        idle = self.get_runner().is_idle()
        finalizing = self.get_runner().is_finalizing(run_id) if run_id is not None else False
        run = self.get_db().get_run(run_id) if run_id is not None else None
        if run_id is not None and run is None:
            raise RuntimeError(f"Run {run_id} is missing")
        failure = self.get_runner().get_run_error(run_id) if run_id is not None else None
        return run, idle, failure, finalizing

    async def finish_foreground(self, run, failure):
        # Terminal DB state alone never releases the slot. Worker is already dead.
        await self._drain()
        failure = self.output_failure() or failure
        if run.get("status") in TASK_RUN_ACTIVE_STATUSES:
            failure = failure or "Worker exited without a final persisted result; reopen Manzara for recovery."
        if failure and self.output_failure():
            await self._show_emergency_notice(f"Failed · {self.foreground.title} · run {run['run_id']} · {elapsed(run)}\n{failure}")
        else:
            await self._say(summary_text(run, self.foreground.title, completion=True, failure=failure))
            await self._drain()
        await self._signal_completion(run, failure=failure)
        self._release_foreground()

    async def finish_without_snapshot(self, read_failure):
        await self._drain()
        failure = await asyncio.to_thread(self.get_runner().get_run_error, self.foreground_run_id)
        text = (f"Failed to read final result · {self.foreground.title} · run {self.foreground_run_id} · "
                f"{duration(max(0, int(time.monotonic() - self.foreground_started)))}\n"
                f"{failure or read_failure}\n"
                "Final counts/outcome are unavailable. Inspect /history after reopening; an unfinished persisted run needs recovery.")
        if self.output_failure():
            await self._show_emergency_notice(text)
        else:
            await self._say(text)
            await self._drain()
        await self._signal_completion(None, failure=failure or read_failure)
        self._release_foreground()
        self.runtime_error = read_failure

    def _release_foreground(self):
        self.foreground = None
        self.foreground_run_id = None
        self.foreground_snapshot = None
        self.stopping = False
        self.finalizing = False
        self.runtime_error = None
        if not self.is_closing():
            self.clear_message()


    async def poll(self) -> bool | None:
        """Refresh one worker snapshot; None discards a stale run identity."""
        try:
            run_id = self.foreground_run_id
            run, idle, failure, finalizing = await asyncio.to_thread(self.snapshot, run_id)
            if run_id != self.foreground_run_id:
                return None
            self.foreground_snapshot = run
            self.finalizing = finalizing
            self.runtime_error = None
            if self.foreground and not self.starting and not self._stop_pending and idle:
                if run is not None:
                    await self.finish_foreground(run, failure)
        except Exception as exc:
            message = "Runtime read failed: " + redact(exc)
            if self.runtime_error != message:
                await self._say(message + ". Current progress is unavailable; /stop or /quit remain available.")
            self.runtime_error = message
            self.foreground_snapshot = None
            # Worker exit and output drain remain knowable without DB reads.
            idle = await asyncio.to_thread(self.get_runner().is_idle)
            if self.foreground and not self.starting and not self._stop_pending and idle:
                await self.finish_without_snapshot(message)
        return idle
