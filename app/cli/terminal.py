"""Inline command prompt with permanent scrollback and explicit run ownership."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
import signal
import sys
import termios
import time

from prompt_toolkit.output import ColorDepth
from prompt_toolkit.utils import get_bell_environment_variable, get_term_environment_variable
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Button, Label, TextArea

from app.cli.commands import COMMANDS, COMMAND_BY_NAME, help_text
from app.cli.display import InlineApplication, TerminalDisplay, emergency_notice
from app.cli.output import OutputFailure, TerminalOutput
from app.cli.presentation import duration, elapsed, progress_text, provider_wait, restrictions, status, summary_text
from app.db import Database
from app.gemini_workers import resolve_gemini_workers
from app.runtime_states import TASK_RUN_ACTIVE_STATUSES, TASK_RUN_STATUS_STARTING
from app.settings import load_settings
from app.task_runtime.contracts import RunOptions
from app.task_runtime.logging import redact
from app.task_runtime.session import SessionLock
from app.tasks import TaskRunner


class Terminal:
    def __init__(self, arguments, descriptor_factory):
        self.arguments = arguments
        self.descriptor_factory = descriptor_factory
        self.db = None
        self.runner = None
        self.session = None
        self.descriptors = []
        self.options = {}
        self.selected_id = arguments.task
        self.ready = False
        self.startup_finished = False
        self.closing = False
        self.stopping = False
        self.starting = False
        self.finalizing = False
        self.foreground = None
        self.foreground_run_id = None
        self.foreground_snapshot = None
        self.foreground_started = 0.0
        self.runtime_error = None
        self.event_cursor = 0
        self.waiting = {}
        self.output = TerminalOutput()
        self.display = TerminalDisplay.from_pty(
            sys.stdout, term=get_term_environment_variable(),
            default_color_depth=ColorDepth.from_env(), enable_bell=get_bell_environment_variable(),
        )
        self._consumer_finished = False
        self._terminal_attributes = None
        self._operations = set()
        self._stop_pending = False
        self.mode = ""
        self.picker_index = 0
        self.include_disabled = False
        self.history_rows = []
        self._picker_generation = 0
        self.message = "Initializing local runtime…"
        self.form_error = ""
        self._recalling_command = False
        self.prompt = TextArea(height=Dimension(min=1, max=3), multiline=False, wrap_lines=True,
                               prompt="› ", history=InMemoryHistory())
        # TextArea forces a one-row window for non-multiline buffers. Retain
        # single-command input while letting wrapped text use a few rows.
        self.prompt.window.height = Dimension(min=1, max=3)
        self.prompt.buffer.on_text_changed += self._prompt_changed
        self.search = TextArea(height=1, multiline=False, prompt="Search › ")
        self.search.buffer.on_text_changed += self._search_changed
        self.workers_input = TextArea(height=1, multiline=False)
        self.limit_input = TextArea(height=1, multiline=False)
        self.save_button = Button("Save", handler=self._save_settings, width=8)
        self.cancel_button = Button("Cancel", handler=self._dismiss, width=8)
        self.picker_control = FormattedTextControl(self._picker_text)
        picker = ConditionalContainer(HSplit([
            Window(FormattedTextControl(self._picker_title), height=1),
            ConditionalContainer(self.search, filter=Condition(lambda: self.mode in ("task", "history"))),
            Window(self.picker_control, height=Dimension(min=1, max=6), wrap_lines=True),
        ]), filter=Condition(lambda: self.mode in ("commands", "task", "history")))
        form = ConditionalContainer(HSplit([
            Label(lambda: f"Settings · {self.task.title if self.task else ''}"),
            Label("Workers (positive integer)"), self.workers_input,
            Label("Candidate limit (blank = unlimited)"), self.limit_input,
            ConditionalContainer(
                Window(FormattedTextControl(lambda: [("class:warning", self.form_error)]),
                       height=Dimension(min=1, max=3), wrap_lines=True),
                filter=Condition(lambda: bool(self.form_error)),
            ),
            VSplit([self.save_button, self.cancel_button], padding=1),
        ]), filter=Condition(lambda: self.mode == "settings"))
        self.layout = Layout(HSplit([
            Window(FormattedTextControl(self._activity_text), height=Dimension(min=1),
                   dont_extend_height=True, wrap_lines=True),
            picker, form,
            ConditionalContainer(self.prompt, filter=Condition(lambda: self.mode not in ("settings", "task", "history"))),
            Window(FormattedTextControl(self._hint_text), height=Dimension(min=1, max=2), wrap_lines=True),
        ]), focused_element=self.prompt)
        self.app = InlineApplication(
            output=self.display,
            layout=self.layout, key_bindings=self._keys(), full_screen=False,
            refresh_interval=0.2, min_redraw_interval=0.05,
            style=Style.from_dict({"selected": "bold ansicyan", "disabled": "ansibrightblack",
                                  "warning": "ansiyellow", "activity": "ansigreen"}),
        )

    @property
    def task(self):
        return next((task for task in self.descriptors if task.task_id == self.selected_id), None)

    @property
    def locked(self):
        return self.foreground is not None or self.closing

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._operations.add(task)
        task.add_done_callback(self._operations.discard)
        return task

    async def _say(self, text):
        try:
            await asyncio.to_thread(self.output.write, redact(text) + "\n")
        except OutputFailure as exc:
            self.message = str(exc)

    def _prompt_changed(self, _buffer):
        if self.mode in ("task", "history", "settings"):
            return
        if self._recalling_command:
            # Recalled slash commands must leave arrows available for history.
            self.mode = ""
            return
        text = self.prompt.text
        self.mode = "commands" if text.startswith("/") and not any(char.isspace() for char in text) else ""
        self.picker_index = 0

    def _search_changed(self, _buffer):
        self.picker_index = 0

    def _picker_items(self):
        if self.mode == "commands":
            query = self.prompt.text.removeprefix("/").lower()
            return [(command, f"/{command.name}  {command.description}", True)
                    for command in COMMANDS if command.name.startswith(query)]
        query = self.search.text.casefold()
        if self.mode == "task":
            return [(task, f"{task.title} · {task.group}" +
                     (f" · disabled: {task.unavailable_reason}" if not task.available else ""), task.available)
                    for task in self.descriptors if (task.available or self.include_disabled)
                    and query in f"{task.title} {task.task_id} {task.group}".casefold()]
        if self.mode == "history":
            return [(run, f"Run {run['run_id']} · {status(run)} · {elapsed(run)} · {run['started_at']}", True)
                    for run in self.history_rows
                    if query in f"{run['run_id']} {status(run)} {run['started_at']}".casefold()]
        return []

    def _picker_title(self):
        titles = {"commands": "Commands", "task": "Tasks · select, then /run",
                  "history": f"Recent runs · {self.task.title if self.task else ''} (latest 20)"}
        return [("", titles.get(self.mode, ""))]

    def _picker_text(self):
        items = self._picker_items()
        if not items:
            return [("class:disabled", "No matches")]
        self.picker_index = min(self.picker_index, len(items) - 1)
        # Keep the selected row visible without a permanent scrolling pane.
        first = self.picker_index
        rows = []
        for index in range(first, min(len(items), first + 5)):
            _, label, enabled = items[index]
            style = "class:selected" if index == self.picker_index else ("" if enabled else "class:disabled")
            rows.append((style, f"{'›' if index == self.picker_index else ' '} {redact(label)}\n"))
        return rows

    def _activity_text(self):
        if self.foreground is None:
            title = self.task.title if self.task else "Manzara"
            return [("", f"  Idle · {redact(title)}")]
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 5) % 10]
        run = self.foreground_snapshot
        if self.finalizing or (run and run.get("status") not in TASK_RUN_ACTIVE_STATUSES):
            phase = "Finalizing"
        elif self.stopping or (run and run.get("stop_mode")):
            phase = "Stopping"
        elif self.starting or (run and run.get("status") == TASK_RUN_STATUS_STARTING):
            phase = "Starting"
        else:
            phase = "Running"
        elapsed_text = duration(max(0, int(time.monotonic() - self.foreground_started)))
        parts = [f"  {spinner} {phase} · {redact(self.foreground.title)}"]
        if self.runtime_error:
            parts.append("progress unavailable · runtime read failed")
        elif run and phase == "Running":
            parts.append(redact(progress_text(run, self.app.output.get_size().columns)))
        parts.append(elapsed_text)
        if not self.runtime_error:
            # Each event describes a worker or request gate, never all workers.
            waits = [text for payload in self.waiting.values() if (text := provider_wait(payload))]
            if waits:
                parts.append(redact(waits[0]) + (f" (+{len(waits) - 1} waits)" if len(waits) > 1 else ""))
        return [("class:warning" if self.runtime_error else "class:activity", "  ·  ".join(parts))]

    def _hint_text(self):
        if self.mode == "settings":
            hint = "Tab move · Enter/Ctrl-S save · Esc cancel"
        elif self.mode in ("task", "history", "commands"):
            hint = "↑↓ select · Enter choose · Esc dismiss"
        elif self.stopping or self.closing:
            hint = "/ commands · Ctrl-C force exit"
        else:
            hint = "/ commands · ↑↓ command history · " + ("Ctrl-C stop" if self.foreground else "Ctrl-C clear / exit")
        return [("class:warning" if self.message else "class:disabled",
                 redact(self.message) if self.message else hint)]

    def _keys(self):
        keys = KeyBindings()
        picking = Condition(lambda: self.mode in ("commands", "task", "history"))
        editing = Condition(lambda: self.mode == "settings")

        @keys.add("up", filter=picking)
        @keys.add("down", filter=picking)
        def move(event):
            count = len(self._picker_items())
            if count:
                self.picker_index = (self.picker_index + (-1 if event.key_sequence[0].key == "up" else 1)) % count

        recalling = ~picking & ~editing & Condition(lambda: self.layout.has_focus(self.prompt))

        @keys.add("up", filter=recalling)
        @keys.add("down", filter=recalling)
        def recall(event):
            self._recalling_command = True
            try:
                if event.key_sequence[0].key == "up":
                    self.prompt.buffer.history_backward()
                else:
                    self.prompt.buffer.history_forward()
            finally:
                self._recalling_command = False

        @keys.add("enter", filter=~editing)
        def enter(event):
            if self.mode in ("task", "history") or (self.mode == "commands" and self._picker_items()):
                self._choose_picker()
            else:
                self._submit_command(self.prompt.text)

        @keys.add("tab", filter=~editing)
        def complete(event):
            if self.mode == "commands":
                self._choose_picker(complete=True)
            elif self.prompt.text == "":
                self.prompt.text = "/"

        @keys.add("tab", filter=editing)
        @keys.add("s-tab", filter=editing)
        def form_tab(event):
            targets = [self.workers_input, self.limit_input, self.save_button, self.cancel_button]
            current = next((i for i, target in enumerate(targets) if self.layout.has_focus(target)), 0)
            step = -1 if event.key_sequence[0].key == "s-tab" else 1
            self.layout.focus(targets[(current + step) % len(targets)])

        @keys.add("c-s", filter=editing)
        def save(event):
            self._save_settings()

        @keys.add("enter", filter=editing)
        def form_enter(event):
            if self.layout.has_focus(self.cancel_button):
                self._dismiss()
            else:
                self._save_settings()

        @keys.add("escape")
        def dismiss(event):
            self._dismiss()

        @keys.add("c-c")
        def interrupt(event):
            self._request_interrupt()

        return keys

    def _dismiss(self):
        self._picker_generation += 1
        self.mode = ""
        self.form_error = ""
        self.message = ""
        self.layout.focus(self.prompt)

    def _choose_picker(self, *, complete=False):
        items = self._picker_items()
        if not items:
            return
        value, _, enabled = items[min(self.picker_index, len(items) - 1)]
        if not enabled:
            self.message = value.unavailable_reason
            self._spawn(self._say("Disabled · " + value.title + ": " + value.unavailable_reason))
            return
        mode = self.mode
        self._dismiss()
        if mode == "commands":
            if complete:
                self.prompt.text = f"/{value.name} "
            else:
                self._submit_command(f"/{value.name}")
        elif mode == "task":
            if self.locked:
                self.message = "Task selection is locked until the foreground run finishes."
                return
            self.selected_id = value.task_id
            self._spawn(self._say(f"Selected · {value.title}. Use /settings or /run."))
        elif mode == "history":
            title = self.task.title
            self._spawn(self._say(summary_text(value, title)))

    def _submit_command(self, text):
        self.prompt.text = text.strip()
        self.prompt.buffer.reset(append_to_history=True)
        self.mode = ""
        self._spawn(self._dispatch(text))

    async def _dispatch(self, text):
        text = text.strip()
        if not text:
            return
        self.message = ""
        parts = text.split(maxsplit=1)
        command = COMMAND_BY_NAME.get(parts[0][1:]) if parts[0].startswith("/") else None
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command is None or argument not in command.arguments:
            await self._say("Use / to choose a command, or /help for guidance. Task selection: /task or /task all.")
            return
        if not self.ready and command.name not in ("help", "quit"):
            await self._say("Runtime is not ready. See the startup message; /help and /quit are available.")
            return
        try:
            await getattr(self, command.handler)(argument)
        except Exception as exc:
            await self._say(f"/{command.name} failed: {redact(exc)}")

    async def _select_task(self, argument):
        if self.locked:
            await self._say("Task selection is locked until the foreground run finishes.")
            return
        self.include_disabled = argument == "all"
        self.mode = "task"
        self.search.text = ""
        self.picker_index = 0
        self.layout.focus(self.search)

    async def _settings(self, _argument):
        if self.locked or not self.task.available:
            await self._say("Settings require an idle, runnable task.")
            return
        options = self.options[self.task.task_id]
        self.workers_input.text = str(options.workers)
        self.limit_input.text = str(options.limit) if options.limit is not None else ""
        self.form_error = ""
        self.mode = "settings"
        self.layout.focus(self.workers_input)

    def _save_settings(self):
        try:
            if self.locked:
                raise ValueError("Settings are locked until the foreground run finishes")
            workers = self.workers_input.text.strip()
            limit = self.limit_input.text.strip()
            if not workers.isascii() or not workers.isdigit():
                raise ValueError("Workers must be a positive integer")
            if limit and (not limit.isascii() or not limit.isdigit()):
                raise ValueError("Limit must be a positive integer or blank")
            maximum = self.task.workers_max
            if maximum is not None and int(workers) > maximum:
                raise ValueError(f"{self.task.title} supports at most {maximum} worker(s)")
            if self.task.task_id == "library.suggest_publisher_merges" and limit:
                raise ValueError("Cluster publishers requires the complete inventory; leave limit blank")
            self.options[self.task.task_id] = replace(
                self.options[self.task.task_id], workers=int(workers), limit=int(limit) if limit else None,
            )
        except ValueError as exc:
            self.form_error = str(exc)
            return
        self._dismiss()
        self._spawn(self._say("Settings saved for the next run. Use /run to start."))

    async def _start(self, _argument):
        if self.locked:
            await self._say("A foreground run is still active or finalizing; wait for its result.")
            return
        task = self.task
        if not task.available:
            await self._say(task.unavailable_reason + " Use /task to select a runnable task.")
            return
        # Claim the slot before any await, including printing or start_task I/O.
        self.foreground = task
        self.starting = True
        self.finalizing = False
        self.stopping = False
        self.foreground_run_id = None
        self.foreground_snapshot = None
        self.foreground_started = time.monotonic()
        self.runtime_error = None
        self.waiting = {}
        options = self.options[task.task_id]
        try:
            if task.task_id == "library.suggest_publisher_merges" and options.limit is not None:
                raise ValueError("Cluster publishers requires the complete inventory; clear the limit in /settings")
            text = f"Starting · {task.title} · workers {options.workers} · limit {options.limit or 'unlimited'}"
            if task.task_id == "library.extract_non_pdf":
                text += "\n" + restrictions(options.as_dict())
            await self._say(text)
            if self.stopping or self.closing:
                await self._say(f"Stopped · {task.title} · start cancelled before execution.")
                await self._drain()
                self._release_foreground()
                return
            result = await asyncio.to_thread(self.runner.start_task, task.task_id, options=options)
            self.foreground_run_id = result["run"]["run_id"]
            self.foreground_snapshot = result["run"]
            if self.stopping or self.closing:
                await asyncio.to_thread(self.runner.stop_task, task.task_id, run_id=self.foreground_run_id)
        except Exception as exc:
            # start_task returns its run identity before doing any further I/O.
            # If stop persistence fails after that, retain the foreground slot.
            await self._say(f"Start/stop failed: {redact(exc)}")
            if self.foreground_run_id is None:
                await self._drain()
                self._release_foreground()
        finally:
            self.starting = False

    async def _stop(self, _argument):
        self._request_stop()

    def _request_stop(self):
        if self.foreground is None:
            self._spawn(self._say("No foreground task is active."))
            return
        if self.stopping:
            return
        self.stopping = True
        self._spawn(self._say("Safe stop requested; active operations finish at checkpoint boundaries. Ctrl-C again forces exit."))
        # During a pending start, _start delivers the stop once its ID arrives.
        # Capture existing IDs now so a delayed stop cannot target a later run.
        if self.foreground_run_id is not None:
            self._spawn(self._deliver_stop(self.foreground.task_id, self.foreground_run_id))

    async def _deliver_stop(self, task_id, run_id):
        self._stop_pending = True
        try:
            if self.runner:
                await asyncio.to_thread(self.runner.stop_task, task_id, run_id=run_id)
        except Exception as exc:
            await self._say("Safe stop recording failed: " + redact(exc) + ". Ctrl-C again forces exit.")
        finally:
            self._stop_pending = False

    async def _history(self, _argument):
        self.mode = "history"
        self.history_rows = []
        self.search.text = ""
        self.picker_index = 0
        self.layout.focus(self.search)
        self._picker_generation += 1
        generation = self._picker_generation
        task_id = self.selected_id
        rows = await asyncio.to_thread(self.db.list_recent_runs_for_task, task_id, limit=20)
        if self.mode == "history" and self._picker_generation == generation:
            self.history_rows = rows

    async def _summary(self, _argument):
        presentation_status = None
        if self.foreground:
            title = self.foreground.title
            run_id = self.foreground_run_id
            if run_id is None:
                phase = "Stopping" if self.stopping else "Starting"
                await self._say(f"{phase} · {title} · no run snapshot yet.")
                return
            run = await asyncio.to_thread(self.db.get_run, run_id)
            if run is None:
                raise RuntimeError(f"Run {run_id} is missing; current progress is unavailable")
            failure = await asyncio.to_thread(self.runner.get_run_error, run_id)
            if self.foreground_run_id == run_id and run.get("status") not in TASK_RUN_ACTIVE_STATUSES:
                presentation_status = "finalizing"
        else:
            title = self.task.title
            run = await asyncio.to_thread(self.db.get_latest_run_for_task, self.selected_id)
            failure = None
        await self._say(summary_text(run, title, failure=failure, presentation_status=presentation_status))

    async def _help(self, _argument):
        await self._say(help_text())

    async def _quit(self, _argument):
        self._request_exit()

    def _request_interrupt(self):
        if self.closing or self.stopping:
            self._force_exit()
        elif self.foreground:
            self._request_stop()
        elif self.mode == "settings" and (self.workers_input.text or self.limit_input.text):
            self._dismiss()
        elif self.mode in ("task", "history") and self.search.text:
            self.search.text = ""
        elif self.prompt.text:
            self.prompt.buffer.reset()
            self._dismiss()
        else:
            self._request_exit()

    def _force_exit(self):
        # Do not fabricate completion. Recovery retains interrupted run semantics.
        try:
            if self._terminal_attributes is not None:
                termios.tcsetattr(self.app.input.fileno(), termios.TCSANOW, self._terminal_attributes)
            # Renderer reset flushes output. Force exit must not wait for a
            # blocked terminal or acquire stdout's buffered-writer lock.
            self.display.emergency_restore()
        finally:
            os._exit(130)

    def _request_exit(self):
        if self.closing:
            return
        self.closing = True
        self._dismiss()
        self.message = "Exiting safely · Ctrl-C again forces exit"
        if self.foreground:
            self.stopping = True
        self._spawn(self._stop_for_exit())

    async def _stop_for_exit(self):
        if self.runner:
            try:
                await asyncio.to_thread(self.runner.request_shutdown)
            except Exception as exc:
                await self._say("Safe exit recording failed: " + redact(exc) + ". Ctrl-C again forces exit.")

    def _snapshot(self, run_id, event_cursor):
        # Determine worker exit *before* reading the final saved result. A
        # terminal row read before exit can still become a finalization failure.
        idle = self.runner.is_idle()
        finalizing = self.runner.is_finalizing(run_id) if run_id is not None else False
        run = self.db.get_run(run_id) if run_id is not None else None
        if run_id is not None and run is None:
            raise RuntimeError(f"Run {run_id} is missing")
        events = self.db.get_events_after(event_cursor, limit=200)
        failure = self.runner.get_run_error(run_id) if run_id is not None else None
        return run, events, idle, failure, finalizing

    def _consume_events(self, events):
        for event in events:
            self.event_cursor = event["event_id"]
            if event["run_id"] != self.foreground_run_id:
                continue
            payload = event["payload"]
            if event["type"] in ("gemini.scheduler.waiting", "gemini.pacing.changed"):
                key = (event["type"], payload.get("worker_id") or payload.get("scope_id"))
                if provider_wait(payload):
                    self.waiting[key] = payload
                else:
                    self.waiting.pop(key, None)
            elif event["type"] == "gemini.key.used":
                worker = payload.get("worker_id")
                if worker:
                    self.waiting.pop(("gemini.scheduler.waiting", worker), None)
        self.waiting = {key: payload for key, payload in self.waiting.items() if provider_wait(payload)}

    async def _drain(self):
        try:
            await asyncio.to_thread(self.output.drain)
        except OutputFailure:
            pass  # The consumer already surfaced the failure and requested stop.

    async def _finish_foreground(self, run, failure):
        # Terminal DB state alone never releases the slot. Worker is already dead.
        await self._drain()
        failure = self.output.failure or failure
        if run.get("status") in TASK_RUN_ACTIVE_STATUSES:
            failure = failure or "Worker exited without a final persisted result; reopen Manzara for recovery."
        if failure and self.output.failure:
            await self._fallback(f"Failed · {self.foreground.title} · run {run['run_id']} · {elapsed(run)}\n{failure}")
        else:
            await self._say(summary_text(run, self.foreground.title, completion=True, failure=failure))
            await self._drain()
        self._release_foreground()

    async def _finish_without_snapshot(self, read_failure):
        await self._drain()
        failure = await asyncio.to_thread(self.runner.get_run_error, self.foreground_run_id)
        text = (f"Failed to read final result · {self.foreground.title} · run {self.foreground_run_id} · "
                f"{duration(max(0, int(time.monotonic() - self.foreground_started)))}\n"
                f"{failure or read_failure}\n"
                "Final counts/outcome are unavailable. Inspect /history after reopening; an unfinished persisted run needs recovery.")
        if self.output.failure:
            await self._fallback(text)
        else:
            await self._say(text)
            await self._drain()
        self._release_foreground()
        self.runtime_error = read_failure

    def _release_foreground(self):
        if self.foreground_run_id is not None and not self.output.failure:
            # Final output has drained; stage one bell through the same writer.
            self.display.bell()
        self.foreground = None
        self.foreground_run_id = None
        self.foreground_snapshot = None
        self.stopping = False
        self.finalizing = False
        self.waiting = {}
        self.runtime_error = None
        if not self.closing:
            self.message = ""

    async def _refresh(self):
        while True:
            if self.ready:
                try:
                    run_id = self.foreground_run_id
                    run, events, idle, failure, finalizing = await asyncio.to_thread(self._snapshot, run_id, self.event_cursor)
                    if run_id != self.foreground_run_id:
                        continue
                    self.foreground_snapshot = run
                    self.finalizing = finalizing
                    self.runtime_error = None
                    self._consume_events(events)
                    if self.foreground and not self.starting and not self._stop_pending and idle:
                        if run is not None:
                            await self._finish_foreground(run, failure)
                    if self.closing and self.startup_finished and not self.foreground and idle:
                        await self._drain()
                        if self.app.is_running:
                            self.app.exit()
                        return
                except Exception as exc:
                    message = "Runtime read failed: " + redact(exc)
                    if self.runtime_error != message:
                        await self._say(message + ". Current progress is unavailable; /stop or /quit remain available.")
                    self.runtime_error = message
                    self.foreground_snapshot = None
                    self.waiting = {}
                    # Worker exit and output drain are knowable without DB
                    # reads. Never strand the UI slot or claim stale success.
                    idle = await asyncio.to_thread(self.runner.is_idle)
                    if self.foreground and not self.starting and not self._stop_pending and idle:
                        await self._finish_without_snapshot(message)
                    if self.closing and self.startup_finished and not self.foreground and idle:
                        await self._drain()
                        if self.app.is_running:
                            self.app.exit()
                        return
            elif self.closing and self.startup_finished:
                await self._drain()
                if self.app.is_running:
                    self.app.exit()
                return
            self.app.invalidate()
            await asyncio.sleep(0.3)

    async def _fallback(self, text):
        emergency_notice(text)

    async def _consume_output(self):
        while not self._consumer_finished:
            batch = ""
            try:
                await self.display.drain()
                self.app.resume_rendering()
                await self.display.drain()
                batch = self.output.take_batch()
                if batch:
                    self.app.print_transcript(batch)
                    await self.display.drain()
                else:
                    await asyncio.sleep(0.05)
            except Exception as exc:
                self.output.fail(exc)
                self.message = self.output.failure
                self._request_exit()
                await self._fallback(self.output.failure + ". Safe stop requested; Ctrl-C again forces exit.")
                return
            finally:
                if batch:
                    self.output.acknowledge()

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
        self.runner = TaskRunner(self.db, descriptors, log_factory=self.output.sink, max_active_runs=1)
        return descriptors

    async def _startup(self):
        try:
            self.descriptors = await asyncio.to_thread(self._initialize)
            self.descriptors.sort(key=lambda item: (item.group, not item.available, item.title))
            if self.task is None:
                raise ValueError(f"Unknown task ID: {self.arguments.task}")
            for task in self.descriptors:
                default = resolve_gemini_workers() if os.environ.get("MANZARA_GEMINI_WORKERS") else task.workers_default
                workers = self.arguments.workers if self.arguments.workers is not None else default
                if task.workers_max == 1:
                    workers = 1
                extraction = task.task_id == "library.extract_non_pdf"
                self.options[task.task_id] = RunOptions(
                    workers, None if task.task_id == "library.suggest_publisher_merges" else self.arguments.limit,
                    per_mime_limit=self.arguments.per_mime_limit if extraction else None,
                    retry_known_failures=self.arguments.retry_known_failures if extraction else False,
                    only_md5s=tuple(dict.fromkeys(self.arguments.only_md5)) if extraction else (),
                )
            self.event_cursor = await asyncio.to_thread(self.db.get_latest_event_id)
            self.ready = True
            self.message = ""
            await self._say(f"Manzara · {self.task.title} selected. /task to change, /settings to edit, /run to start.")
            if not self.task.available:
                await self._say("Selected task disabled: " + self.task.unavailable_reason)
            if self.closing:
                await asyncio.to_thread(self.runner.request_shutdown)
        except Exception as exc:
            self.message = "Startup failed: " + redact(exc) + " · /quit to exit"
            await self._say(self.message)
        finally:
            self.startup_finished = True

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        self._terminal_attributes = termios.tcgetattr(self.app.input.fileno())
        previous_handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
        loop.add_signal_handler(signal.SIGINT, self._request_interrupt)
        loop.add_signal_handler(signal.SIGTERM, self._request_exit)
        startup = consumer = refresh = None

        def begin():
            nonlocal startup, consumer, refresh
            # Keep one consumer for renderer frames and transcript messages.
            consumer = asyncio.create_task(self._consume_output())
            startup = asyncio.create_task(self._startup())
            refresh = asyncio.create_task(self._refresh())

        try:
            await self.app.run_async(pre_run=begin, handle_sigint=False)
        finally:
            self.closing = True
            if refresh:
                refresh.cancel()
                await asyncio.gather(refresh, return_exceptions=True)
            # Initialization must finish before resource cleanup; the output
            # consumer remains alive while workers/operations finish off-loop.
            try:
                if startup:
                    await startup
                if self.runner:
                    await asyncio.to_thread(self.runner.request_shutdown)
            finally:
                try:
                    if self._operations:
                        await asyncio.gather(*tuple(self._operations), return_exceptions=True)
                    if self.runner:
                        await asyncio.to_thread(self.runner.shutdown)
                    if consumer:
                        await self._drain()
                finally:
                    self._consumer_finished = True
                    try:
                        if consumer:
                            await consumer
                        try:
                            # Include the toolkit's final cursor/style reset.
                            await self.display.drain()
                        except OutputFailure as exc:
                            self.output.fail(exc)
                            await self._fallback(self.output.failure)
                    finally:
                        self.display.close()
                        try:
                            if self.db:
                                await asyncio.to_thread(self.db.close)
                        finally:
                            try:
                                if self.session:
                                    self.session.close()
                            finally:
                                for signum, handler in previous_handlers.items():
                                    loop.remove_signal_handler(signum)
                                    signal.signal(signum, handler)
        return 1 if self.output.failure else 0
