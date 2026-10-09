"""Inline asynchronous terminal interaction; all I/O runs off the UI loop."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import os
import signal
import termios

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Label, TextArea

from app.db import Database
from app.gemini_workers import resolve_gemini_workers
from app.runtime_states import TASK_RUN_ACTIVE_STATUSES, TASK_RUN_STATUS_COMPLETED
from app.settings import load_settings
from app.task_runtime.contracts import RunOptions
from app.task_runtime.logging import redact
from app.task_runtime.session import SessionLock
from app.tasks import TaskRunner


_ACTIONS = ("Start / Resume", "Stop safely", "Settings", "Logs", "Summary", "Recent runs", "Back")


def _elapsed(run: dict) -> str:
    try:
        start = datetime.fromisoformat(run["started_at"])
        end = datetime.fromisoformat(run["finished_at"]) if run.get("finished_at") else datetime.now(timezone.utc)
        seconds = max(0, int((end - start).total_seconds()))
        return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    except (KeyError, TypeError, ValueError):
        return "--:--:--"


def _status(run: dict) -> str:
    summary = run.get("summary") or {}
    return "deferred" if run.get("status") == TASK_RUN_STATUS_COMPLETED and summary.get("outcome") == "deferred" else str(run.get("status", "idle"))


class Terminal:
    def __init__(self, arguments, descriptor_factory):
        self.arguments = arguments
        self.descriptor_factory = descriptor_factory
        self.db = None
        self.runner = None
        self.session = None
        self.descriptors = []
        self.options = {}
        self.latest = {}
        self.recent = []
        self.selected = 0
        self.action = 0
        self.run_index = 0
        self.inspected_run_id = None
        self.view = "details"
        self.ready = False
        self.startup_finished = False
        self.closing = False
        self._terminal_attributes = None
        self.pending = False
        self.message = "Initializing local runtime…"
        self.event_cursor = 0
        self.waiting = {}
        self.logs = []
        self.follow = True
        self.tasks_control = FormattedTextControl(self._tasks_text, focusable=True,
                                                 get_cursor_position=lambda: Point(0, self.selected))
        self.actions_control = FormattedTextControl(self._actions_text, focusable=True,
                                                   get_cursor_position=lambda: Point(0, self.action))
        self.runs_control = FormattedTextControl(self._runs_text, focusable=True,
                                                get_cursor_position=lambda: Point(0, self.run_index))
        self.detail = TextArea(read_only=True, scrollbar=True, height=8, wrap_lines=True)
        self.workers_input = TextArea(height=1, multiline=False)
        self.limit_input = TextArea(height=1, multiline=False)
        normal = ConditionalContainer(HSplit([
            Window(self.tasks_control, height=6),
            ConditionalContainer(Window(self.actions_control, height=3),
                                 filter=Condition(lambda: self.view != "settings")),
            ConditionalContainer(Window(self.runs_control, height=4),
                                 filter=Condition(lambda: self.view == "history")),
            self.detail,
        ]), filter=Condition(lambda: self.view != "settings"))
        settings = ConditionalContainer(HSplit([
            Label("Settings for the next run"), Label("Workers (positive integer)"), self.workers_input,
            Label("Candidate limit (blank = unlimited)"), self.limit_input,
            Label("Tab: next field  Enter/Ctrl-S: save  Esc: cancel"),
        ]), filter=Condition(lambda: self.view == "settings"))
        self.layout = Layout(HSplit([
            Label("Manzara  ·  task operations"),
            Window(FormattedTextControl(self._active_text), height=3),
            normal, settings,
            Window(FormattedTextControl(lambda: [("class:message", redact(self.message))]), height=2, wrap_lines=True),
            Label("↑↓ navigate  Enter actions  Tab focus  Esc back  q/Ctrl-C stop and exit  Ctrl-C again: force exit"),
        ]), focused_element=self.tasks_control)
        self.app = Application(
            layout=self.layout, key_bindings=self._keys(), full_screen=False,
            refresh_interval=0.25, min_redraw_interval=0.05,
            style=Style.from_dict({"selected": "bold ansicyan", "disabled": "ansibrightblack",
                                  "message": "ansiyellow", "active": "ansigreen"}),
        )

    @property
    def task(self):
        return self.descriptors[self.selected] if self.descriptors else None

    @property
    def selected_run(self):
        if self.inspected_run_id is not None:
            return next((run for run in self.recent if run["run_id"] == self.inspected_run_id), None)
        return self.recent[min(self.run_index, len(self.recent) - 1)] if self.recent else None

    def _tasks_text(self):
        if not self.descriptors:
            return [("", "Loading task registry…")]
        rows = []
        for index, task in enumerate(self.descriptors):
            run = self.latest.get(task.task_id) or {}
            marker = "›" if index == self.selected else " "
            state = _status(run) if task.available else "unavailable"
            style = "class:selected" if index == self.selected else ("" if task.available else "class:disabled")
            rows.append((style, f"{marker} {task.group} / {task.title}  [{state}]\n"))
        return rows

    def _actions_text(self):
        if not self.ready:
            return [("", "")]
        return [("class:selected" if index == self.action else "", f"{'›' if index == self.action else ' '} {label}\n")
                for index, label in enumerate(_ACTIONS)]

    def _runs_text(self):
        return [("class:selected" if index == self.run_index else "",
                 f"{'›' if index == self.run_index else ' '} run {run['run_id']}  {_status(run)}  {_elapsed(run)}\n")
                for index, run in enumerate(self.recent)] or [("", "No runs yet")]

    def _progress_text(self, run):
        progress = run.get("progress") or {}
        if run.get('task_id') == 'library.extract_non_pdf' and 'total' in progress:
            return (f"{progress.get('current', 0)}/{progress['total']} examined · "
                    f"{progress.get('ready', 0)} ready · {progress.get('failed', 0)} failed · "
                    f"{progress.get('deferred', 0)} deferred")
        if progress.get("phase") == "discovering":
            if run.get("status") not in TASK_RUN_ACTIVE_STATUSES:
                return "Candidate discovery did not finish."
            spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(asyncio.get_running_loop().time() * 4) % 10]
            return f"{spinner} discovering candidates"
        total = progress.get("total", 0)
        resolved = progress.get("resolved", max(0, progress.get("current", 0) - progress.get("deferred", 0)))
        ratio = min(1, resolved / total) if total else (1 if run.get("status") == TASK_RUN_STATUS_COMPLETED else 0)
        filled = int(16 * ratio)
        return f"[{'━' * filled}{'·' * (16 - filled)}] {resolved}/{total} resolved · {progress.get('processed', 0)} processed"

    def _active_text(self):
        active = [run for run in self.latest.values() if run and run.get("status") in TASK_RUN_ACTIVE_STATUSES]
        if not active:
            return [("", "Active runs: none" + (" · shutting down" if self.closing else ""))]
        return [("class:active", f"{run['task_id']}  {_status(run)}  {self._progress_text(run)}  {_elapsed(run)}\n")
                for run in active]

    def _keys(self):
        keys = KeyBindings()
        editing = Condition(lambda: self.view == "settings")
        navigating = Condition(lambda: self.layout.has_focus(self.tasks_control) or self.layout.has_focus(self.actions_control)
                               or self.layout.has_focus(self.runs_control))

        @keys.add("up", filter=~editing & navigating)
        @keys.add("down", filter=~editing & navigating)
        def move(event):
            delta = -1 if event.key_sequence[0].key == "up" else 1
            if self.layout.has_focus(self.actions_control):
                self.action = (self.action + delta) % len(_ACTIONS)
            elif self.layout.has_focus(self.runs_control):
                self.run_index = max(0, min(len(self.recent) - 1, self.run_index + delta))
                if self.recent:
                    self.inspected_run_id = self.recent[self.run_index]["run_id"]
            elif self.descriptors:
                self.selected = (self.selected + delta) % len(self.descriptors)
                self.run_index = 0
                self.inspected_run_id = None
                self.recent = []
                self.logs = []
                self.view = "details"

        @keys.add("enter", filter=~editing & navigating)
        def enter(event):
            if self.layout.has_focus(self.tasks_control):
                self.layout.focus(self.actions_control)
            elif self.layout.has_focus(self.runs_control):
                self.view = "summary"
                self.layout.focus(self.detail)
            else:
                self.app.create_background_task(self._action())

        @keys.add("tab")
        def tab(event):
            if self.view == "settings":
                self.layout.focus(self.limit_input if self.layout.has_focus(self.workers_input) else self.workers_input)
            else:
                targets = [self.tasks_control, self.actions_control,
                           self.runs_control if self.view == "history" else self.detail]
                current = next((i for i, target in enumerate(targets) if self.layout.has_focus(target)), -1)
                self.layout.focus(targets[(current + 1) % len(targets)])

        @keys.add("escape")
        def back(event):
            self.view = "details"
            self.layout.focus(self.tasks_control)

        @keys.add("c-s", filter=editing)
        @keys.add("enter", filter=editing)
        def save(event):
            self._save_settings()

        @keys.add("pageup", filter=Condition(lambda: self.view == "logs"))
        @keys.add("pagedown", filter=Condition(lambda: self.view == "logs"))
        def page(event):
            self.app.create_background_task(self._log_page(older=event.key_sequence[0].key == "pageup"))

        @keys.add("f", filter=Condition(lambda: self.view == "logs"))
        def follow(event):
            self.follow = True
            self.logs = []

        @keys.add("q", filter=~editing)
        def quit_(event):
            self._request_exit()

        @keys.add("c-c")
        def interrupt(event):
            self._request_interrupt()

        return keys

    def _request_interrupt(self):
        if self.closing:
            self._force_exit()
        else:
            self._request_exit()

    def _force_exit(self):
        # Worker threads and executor shutdown can block indefinitely. Restore
        # terminal state, then let process exit release connections and the lock.
        # Leave active runs for startup recovery rather than claiming completion.
        try:
            if self._terminal_attributes is not None:
                termios.tcsetattr(self.app.input.fileno(), termios.TCSANOW, self._terminal_attributes)
            self.app.renderer.reset()
        finally:
            os._exit(130)

    def _request_exit(self):
        if self.closing:
            return
        self.closing = True
        self.message = "Stopping safely; waiting for active requests and checkpoints. Press Ctrl-C again to force exit."
        self.app.create_background_task(self._stop_for_exit())

    async def _stop_for_exit(self):
        if self.runner is not None:
            try:
                await asyncio.to_thread(self.runner.request_shutdown)
            except Exception as exc:
                self.message = redact(exc) + " · Ctrl-C again to force exit"

    def _save_settings(self):
        try:
            if (self.latest.get(self.task.task_id) or {}).get("status") in TASK_RUN_ACTIVE_STATUSES:
                raise ValueError("Settings are locked while the task is active")
            workers = self.workers_input.text.strip()
            limit = self.limit_input.text.strip()
            if not workers.isascii() or not workers.isdigit():
                raise ValueError("Workers must be a positive integer")
            if limit and (not limit.isascii() or not limit.isdigit()):
                raise ValueError("Limit must be a positive integer or blank")
            maximum = self.task.workers_max
            if maximum is not None and int(workers) > maximum:
                raise ValueError(f"{self.task.title} supports at most {maximum} worker(s)")
            self.options[self.task.task_id] = replace(
                self.options[self.task.task_id], workers=int(workers), limit=int(limit) if limit else None,
            )
        except ValueError as exc:
            self.message = str(exc)
            return
        self.view = "details"
        self.layout.focus(self.actions_control)
        self.message = "Settings saved for the next run."

    async def _action(self):
        if not self.ready or self.pending or self.closing or self.task is None:
            return
        task = self.task
        action = _ACTIONS[self.action]
        active = (self.latest.get(task.task_id) or {}).get("status") in TASK_RUN_ACTIVE_STATUSES
        if action == "Settings":
            if not task.available or active:
                self.message = "Settings are unavailable while this task is disabled or active."
                return
            options = self.options[task.task_id]
            self.workers_input.text = str(options.workers)
            self.limit_input.text = str(options.limit) if options.limit is not None else ""
            self.view = "settings"
            self.layout.focus(self.workers_input)
            return
        if action in ("Logs", "Summary", "Recent runs"):
            self.view = {"Logs": "logs", "Summary": "summary", "Recent runs": "history"}[action]
            self.logs = []
            self.follow = True
            self.layout.focus(self.runs_control if self.view == "history" else self.detail)
            return
        if action == "Back":
            self.view = "details"
            self.layout.focus(self.tasks_control)
            return
        if not task.available:
            self.message = task.unavailable_reason
            return
        self.pending = True
        try:
            if action == "Start / Resume":
                result = await asyncio.to_thread(self.runner.start_task, task.task_id, options=self.options[task.task_id])
                if self.task and self.task.task_id == task.task_id:
                    self.run_index = 0
                    self.inspected_run_id = None
                self.message = "Already running." if result["action"] == "noop" else "Run started. Controls remain available."
            else:
                await asyncio.to_thread(self.runner.stop_task, task.task_id)
                self.message = "Safe stop requested; active requests finish at their checkpoint boundary."
        except Exception as exc:
            self.message = redact(exc)
        finally:
            self.pending = False

    async def _log_page(self, *, older):
        run = self.selected_run
        if run is None or not self.logs:
            return
        self.follow = False
        options = {"before_log_id": self.logs[0]["log_id"]} if older else {"after_log_id": self.logs[-1]["log_id"]}
        try:
            rows = await asyncio.to_thread(self.runner.get_run_logs, task_id=run["task_id"], run_id=run["run_id"], limit=100, **options)
            if self.view == "logs" and self.selected_run and self.selected_run["run_id"] == run["run_id"]:
                if rows:
                    self.logs = rows
                self._render_details()
        except Exception as exc:
            self.message = "Log read failed: " + redact(exc)

    def _snapshot(self, task_id):
        latest = {task.task_id: self.db.get_latest_run_for_task(task.task_id) for task in self.descriptors}
        recent = self.db.list_recent_runs_for_task(task_id, limit=20) if task_id else []
        events = self.db.get_events_after(self.event_cursor, limit=200)
        return latest, recent, events

    def _summary_text(self, run):
        if run is None:
            return "No run summary yet."
        summary = run.get("summary") or {}
        options = summary.get("options") or {}
        lines = [f"{self.task.title} · run {run['run_id']}",
                 f"Status: {_status(run)} · elapsed {_elapsed(run)}",
                 f"Workers: {options.get('workers', run.get('gemini_workers') or 1)} · candidate limit: {options.get('limit') or 'unlimited'}"]
        if summary.get("message"):
            lines.append(str(summary["message"]))
        if summary.get("kind") == "library.personality_normalization_summary":
            lines.append(f"Source names: {summary.get('total', 0)} · eligible: {summary.get('eligible_total', 0)} · skipped: {summary.get('skipped', 0)}")
            for label, key in (("Normalized", "succeeded"), ("Not people", "not_person"),
                               ("Unusable", "unusable"), ("Failed", "failed"),
                               ("Deferred", "deferred"), ("Remaining", "remaining")):
                lines.append(f"{label}: {summary.get(key, 0)}")
            attempts = summary.get("model_attempts") or {}
            if attempts:
                lines.append("Model attempts: " + ", ".join(f"{model}: {count}" for model, count in attempts.items()))
        if summary.get("kind") == "library.non_pdf_extraction_summary":
            for label, key in (("Candidates", "total"), ("Processed", "processed"), ("Ready", "ready"),
                               ("Failed", "failed"), ("Deferred", "deferred"), ("Unsupported", "unsupported"),
                               ("Corrupt plans", "corrupted"), ("Checkpoint conflicts", "checkpoint_raced")):
                lines.append(f"{label}: {summary.get(key, 0)}")
            lines.append("Workspace: " + str(summary.get("workspace_path", "")))
            lines.append(f"MIME cohort cap: {options.get('per_mime_limit') or 'unlimited'} · explicit retries: {options.get('retry_known_failures', False)}")
            if options.get("only_md5s"):
                lines.append("Source cohort: " + ", ".join(options["only_md5s"]))
        error = summary.get("error") or run.get("error_text")
        if error:
            lines.append("Error: " + str(error))
        if summary.get("log_path"):
            lines.append("Log: " + summary["log_path"])
        return redact("\n".join(lines))

    def _render_details(self):
        task = self.task
        if task is None or self.view == "settings":
            return
        run = self.selected_run
        if self.view == "logs":
            text = "\n".join(redact(row["line"]) for row in self.logs) or "No log lines yet."
            text = ("Following logs · PageUp: older · PageDown: newer · f: follow\n" if self.follow
                    else "Log page · PageUp: older · PageDown: newer · f: follow\n") + text
        elif self.view == "summary":
            text = self._summary_text(run)
        else:
            text = f"{task.title}\n"
            if not task.available:
                text += task.unavailable_reason + "\n"
            else:
                options = self.options[task.task_id]
                text += f"Next run: {options.workers} worker(s), limit {options.limit or 'unlimited'}\n"
                if task.task_id == "library.extract_non_pdf":
                    text += (f"MIME cohort cap: {options.per_mime_limit or 'unlimited'} · "
                             f"explicit retries: {options.retry_known_failures}\n")
                    if options.only_md5s:
                        text += "Source cohort: " + ", ".join(options.only_md5s) + "\n"
            if run:
                progress = run.get("progress") or {}
                text += f"Run {run['run_id']}: {_status(run)} · elapsed {_elapsed(run)}\n{self._progress_text(run)}\n"
                names = (('created', 'updated', 'unchanged', 'published', 'cleanups_completed', 'failed')
                         if run.get('task_id') == 'maintenance.monocorpus_sync'
                         else ('ready', 'failed', 'deferred', 'unsupported', 'corrupted', 'checkpoint_raced')
                         if run.get('task_id') == 'library.extract_non_pdf'
                         else ('succeeded', 'not_person', 'unusable', 'failed', 'deferred', 'retry_pending', 'skipped'))
                text += "  ".join(f"{name}: {progress.get(name, 0)}" for name in names)
                waiting = self.waiting.get(run["run_id"])
                if waiting and run["status"] in TASK_RUN_ACTIVE_STATUSES:
                    text += f"\nProvider: {waiting}"
                if run.get("error_text"):
                    text += "\n" + redact(run["error_text"])
            else:
                text += "No previous runs."
        if self.detail.text != text:
            cursor = len(text) if self.view == "logs" and self.follow else min(self.detail.buffer.cursor_position, len(text))
            self.detail.buffer.set_document(Document(text, cursor_position=cursor), bypass_readonly=True)

    async def _refresh(self):
        while True:
            if self.ready:
                task_id = self.task.task_id if self.task else None
                try:
                    latest, recent, events = await asyncio.to_thread(self._snapshot, task_id)
                    self.latest = latest
                    if self.task and self.task.task_id == task_id:
                        self.recent = recent
                    for event in events:
                        self.event_cursor = event["event_id"]
                        payload = event["payload"]
                        if event["type"] == "gemini.scheduler.waiting":
                            self.waiting[event["run_id"]] = f"waiting until {payload.get('wait_until', 'capacity is available')}"
                        elif event["type"] == "gemini.pacing.changed":
                            self.waiting[event["run_id"]] = f"{payload.get('mode')} · interval {payload.get('interval_seconds')}s · {payload.get('wait_until') or 'ready'}"
                        elif event["type"] == "gemini.key.used":
                            self.waiting.pop(event["run_id"], None)
                    if self.view == "logs" and self.follow and self.selected_run:
                        run = self.selected_run
                        options = {"after_log_id": self.logs[-1]["log_id"]} if self.logs else {"tail": True}
                        rows = await asyncio.to_thread(self.runner.get_run_logs, task_id=run["task_id"], run_id=run["run_id"], limit=100, **options)
                        if self.view == "logs" and self.follow and self.selected_run and self.selected_run["run_id"] == run["run_id"]:
                            self.logs = (self.logs + rows)[-100:]
                    self._render_details()
                except Exception as exc:
                    self.message = "Runtime read failed: " + redact(exc)
                if self.closing and not self.pending and await asyncio.to_thread(self.runner.is_idle):
                    self.app.exit()
                    return
            elif self.closing and self.startup_finished:
                self.app.exit()
                return
            await asyncio.sleep(0.5)

    def _initialize(self):
        settings = load_settings()
        self.session = SessionLock(settings.local_state_path)
        self.session.acquire()
        self.db = Database(settings.database_url, schema=settings.database_schema,
                           pool_size=settings.database_pool_size, local_state_path=settings.local_state_path)
        self.db.init_local_state()
        descriptors = self.descriptor_factory()
        recovered = self.db.recover_active_runs()
        if recovered:
            self.db.insert_event("system.recovery", None, None, None, {"recovered_runs": recovered})
        self.runner = TaskRunner(self.db, descriptors)
        return descriptors

    async def _startup(self):
        try:
            self.descriptors = await asyncio.to_thread(self._initialize)
            self.descriptors.sort(key=lambda item: (item.group, not item.available, item.title))
            selected = next((index for index, task in enumerate(self.descriptors)
                             if task.task_id == self.arguments.task), None)
            if selected is None:
                raise ValueError(f"Unknown task ID: {self.arguments.task}")
            self.selected = selected
            for task in self.descriptors:
                default = resolve_gemini_workers() if os.environ.get("MANZARA_GEMINI_WORKERS") else task.workers_default
                workers = self.arguments.workers if self.arguments.workers is not None else default
                if task.workers_max == 1:
                    workers = 1
                extraction = task.task_id == "library.extract_non_pdf"
                self.options[task.task_id] = RunOptions(
                    workers, self.arguments.limit,
                    per_mime_limit=self.arguments.per_mime_limit if extraction else None,
                    retry_known_failures=self.arguments.retry_known_failures if extraction else False,
                    only_md5s=tuple(dict.fromkeys(self.arguments.only_md5)) if extraction else (),
                )
            self.event_cursor = await asyncio.to_thread(self.db.get_latest_event_id)
            self.ready = True
            self.message = "Select a task with arrows, then press Enter."
            if self.closing:
                await asyncio.to_thread(self.runner.request_shutdown)
        except Exception as exc:
            self.message = "Startup failed: " + redact(exc) + " · q/Ctrl-C to exit"
        finally:
            self.startup_finished = True

    async def run(self):
        loop = asyncio.get_running_loop()
        self._terminal_attributes = termios.tcgetattr(self.app.input.fileno())
        previous_handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
        loop.add_signal_handler(signal.SIGINT, self._request_interrupt)
        loop.add_signal_handler(signal.SIGTERM, self._request_exit)
        startup = asyncio.create_task(self._startup())
        refresh = asyncio.create_task(self._refresh())
        try:
            await self.app.run_async(handle_sigint=False)
        finally:
            refresh.cancel()
            await asyncio.gather(refresh, return_exceptions=True)
            # Initialization runs in a thread and must finish before resources are closed.
            await startup
            try:
                if self.runner is not None:
                    await asyncio.to_thread(self.runner.shutdown)
            finally:
                try:
                    if self.db is not None:
                        await asyncio.to_thread(self.db.close)
                finally:
                    if self.session is not None:
                        self.session.close()
                    for signum, handler in previous_handlers.items():
                        loop.remove_signal_handler(signum)
                        signal.signal(signum, handler)
