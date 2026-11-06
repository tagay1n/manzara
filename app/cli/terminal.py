"""Inline command prompt with permanent scrollback and explicit run ownership."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import termios
import time
from dataclasses import replace

from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    HSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import (
    get_bell_environment_variable,
    get_term_environment_variable,
)
from prompt_toolkit.widgets import TextArea

from app.cli.commands import help_text, parse_command
from app.cli.display import InlineApplication, TerminalDisplay, emergency_notice
from app.cli.output import OutputFailure, TerminalOutput
from app.cli.picker import Picker
from app.cli.lifecycle import ForegroundLifecycle
from app.cli.presentation import (
    duration,
    progress_text,
    provider_wait,
    summary_text,
)
from app.cli.state import CLIStateStore, append_command
from app.cli.settings import configured_task_limit
from app.cli.settings_form import SettingsForm
from app.task_settings import configured_task_options
from app.db import Database
from app.gemini_config import load_gemini_keys, load_required_gemini_model_pool
from app.migrations import apply_catalog_migrations
from app.runtime_config import (
    config_integer,
    config_number,
    config_text,
    load_runtime_config,
    runtime_config_snapshot,
    save_runtime_config,
)
from app.runtime_states import TASK_RUN_ACTIVE_STATUSES, TASK_RUN_STATUS_STARTING
from app.settings import load_settings
from app.task_runtime.contracts import RunOptions
from app.task_runtime.logging import redact
from app.task_runtime.reporting import COMPLETION_BELL_INTERVAL_SECONDS, completion_bell_count
from app.task_runtime.session import SessionLock
from app.tasks import TaskRunner


class Terminal:
    def __init__(self, arguments, descriptor_factory):
        self.arguments = arguments
        self.descriptor_factory = descriptor_factory
        self.db = None
        self.runner = None
        self.session = None
        self.cli_state = None
        self._cli_state_lock = asyncio.Lock()
        self._cli_history_loaded = False
        self._command_history = []
        self._startup_commands = []
        self.descriptors = []
        self.options = {}
        self.selected_id = arguments.task
        self.ready = False
        self.startup_finished = False
        self.closing = False
        self.output = TerminalOutput()
        self.display = TerminalDisplay.from_pty(
            sys.stdout, term=get_term_environment_variable(),
            default_color_depth=ColorDepth.from_env(), enable_bell=get_bell_environment_variable(),
        )
        self.lifecycle = ForegroundLifecycle(
            get_runner=lambda: self.runner, get_db=lambda: self.db,
            is_closing=lambda: self.closing, output_failure=lambda: self.output.failure,
            say=self._say, spawn=self._spawn, drain=self._drain,
            signal_completion=self._signal_completion, emergency_notice=self._show_emergency_notice,
            clear_message=self._clear_message,
        )
        self._consumer_finished = False
        self._terminal_attributes = None
        self._operations = set()
        self.mode = ""
        self.history_rows = []
        self._picker_generation = 0
        self.message = "Initializing runtime and applying database migrations…"
        self.settings_busy = False
        self.settings_snapshot = None
        self._recalling_command = False
        self.prompt = TextArea(height=Dimension(min=1, max=3), multiline=False, wrap_lines=True,
                               prompt="› ", history=InMemoryHistory())
        # TextArea forces a one-row window for non-multiline buffers. Retain
        # single-command input while letting wrapped text use a few rows.
        self.prompt.window.height = Dimension(min=1, max=3)
        self.prompt.buffer.on_text_changed += self._prompt_changed
        self.picker = Picker(
            mode=lambda: self.mode, command_text=lambda: self.prompt.text,
            descriptors=lambda: self.descriptors, history_rows=lambda: self.history_rows,
            task_title=lambda: self.task.title if self.task else "",
        )
        self.settings_form = SettingsForm(
            busy=lambda: self.settings_busy, columns=lambda: self.app.output.get_size().columns,
            has_focus=lambda target: self.layout.has_focus(target), focus=lambda target: self.layout.focus(target),
            invalidate=lambda: self.app.invalidate(), save=self._save_settings, cancel=self._dismiss,
        )
        form = ConditionalContainer(self.settings_form.container,
                                    filter=Condition(lambda: self.mode == "settings"))
        self.layout = Layout(HSplit([
            Window(FormattedTextControl(self._activity_text), height=Dimension(min=1),
                   dont_extend_height=True, wrap_lines=True),
            self.picker.container, form,
            ConditionalContainer(self.prompt, filter=Condition(lambda: self.mode not in ("settings", "task", "history"))),
            Window(FormattedTextControl(self._hint_text), height=Dimension(min=1, max=2), wrap_lines=True),
        ]), focused_element=self.prompt)
        self.app = InlineApplication(
            output=self.display,
            layout=self.layout, key_bindings=self._keys(), full_screen=False,
            refresh_interval=0.2, min_redraw_interval=0.05,
            style=Style.from_dict({"selected": "bold ansicyan", "disabled": "ansibrightblack",
                                  "warning": "ansiyellow", "activity": "ansigreen", "setting": "bg:ansibrightblack"}),
        )

    def _clear_message(self):
        self.message = ""

    @property
    def task(self):
        return next((task for task in self.descriptors if task.task_id == self.selected_id), None)

    @property
    def locked(self):
        return self.lifecycle.foreground is not None or self.closing or self.settings_busy

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

    async def _persist_cli_state(self, item_id, payload):
        async with self._cli_state_lock:
            try:
                await asyncio.to_thread(self.cli_state.save, item_id, payload)
            except Exception as exc:
                await self._say(
                    f"Could not save CLI {item_id}: {redact(exc)}. "
                    "Check the configured local_state_path and its permissions; "
                    "this change may not survive restart."
                )

    def _remember_command(self, text):
        if not self._cli_history_loaded:
            self._startup_commands.append(text)
        elif append_command(self._command_history, text):
            self._spawn(self._persist_cli_state("commands", {"entries": list(self._command_history)}))

    def _prompt_changed(self, _buffer):
        if self.mode in ("task", "history", "settings"):
            return
        if self._recalling_command:
            # Recalled slash commands must leave arrows available for history.
            self.mode = ""
            return
        text = self.prompt.text
        self.mode = "commands" if text.startswith("/") and not any(char.isspace() for char in text) else ""
        self.picker.index = 0

    def _activity_text(self):
        if self.lifecycle.foreground is None:
            title = self.task.title if self.task else "Manzara"
            return [("", f"  Idle · {redact(title)}")]
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.monotonic() * 5) % 10]
        run = self.lifecycle.foreground_snapshot
        if self.lifecycle.finalizing or (run and run.get("status") not in TASK_RUN_ACTIVE_STATUSES):
            phase = "Finalizing"
        elif self.lifecycle.stopping or (run and run.get("stop_mode")):
            phase = "Stopping"
        elif self.lifecycle.starting or (run and run.get("status") == TASK_RUN_STATUS_STARTING):
            phase = "Starting"
        else:
            phase = "Running"
        elapsed_text = duration(max(0, int(time.monotonic() - self.lifecycle.foreground_started)))
        parts = [f"  {spinner} {phase} · {redact(self.lifecycle.foreground.title)}"]
        if self.lifecycle.runtime_error:
            parts.append("progress unavailable · runtime read failed")
        elif run and phase == "Running":
            parts.append(redact(progress_text(run, self.app.output.get_size().columns)))
        parts.append(elapsed_text)
        if not self.lifecycle.runtime_error and run:
            wait = provider_wait(run.get("provider_wait") or {})
            if wait:
                parts.append(redact(wait))
        return [("class:warning" if self.lifecycle.runtime_error else "class:activity", "  ·  ".join(parts))]

    def _hint_text(self):
        if self.mode == "settings":
            hint = "Tab move · ↑↓ adjust · Enter next · Ctrl-S save · Esc cancel"
        elif self.mode in ("task", "history", "commands"):
            hint = "↑↓ select · Enter choose · Esc dismiss"
        elif self.lifecycle.stopping or self.closing:
            hint = "/ commands · Ctrl-C force exit"
        else:
            hint = "/ commands · ↑↓ command history · " + ("Ctrl-C stop" if self.lifecycle.foreground else "Ctrl-C clear / exit")
        return [("class:warning" if self.message else "class:disabled",
                 redact(self.message) if self.message else hint)]

    def _keys(self):
        keys = KeyBindings()
        picking = Condition(lambda: self.mode in ("commands", "task", "history"))
        editing = Condition(lambda: self.mode == "settings")

        @keys.add("up", filter=picking)
        @keys.add("down", filter=picking)
        def move(event):
            self.picker.move(-1 if event.key_sequence[0].key == "up" else 1)

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
            if self.mode in ("task", "history") or (self.mode == "commands" and self.picker.items()):
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
            self.settings_form.move_focus(-1 if event.key_sequence[0].key == "s-tab" else 1)

        @keys.add("c-s", filter=editing)
        def save(event):
            self._save_settings()

        @keys.add("enter", filter=editing)
        def form_enter(event):
            self.settings_form.enter()

        @keys.add("up", filter=editing)
        @keys.add("down", filter=editing)
        def form_adjust(event):
            self.settings_form.adjust(-1 if event.key_sequence[0].key == "down" else 1)

        @keys.add("escape")
        def dismiss(event):
            self._dismiss()

        @keys.add("c-c")
        def interrupt(event):
            self._request_interrupt()

        return keys

    def _dismiss(self):
        if self.settings_busy:
            return
        self._picker_generation += 1
        self.mode = ""
        self.settings_form.error = ""
        self.message = ""
        self.layout.focus(self.prompt)

    def _choose_picker(self, *, complete=False):
        items = self.picker.items()
        if not items:
            return
        value, _ = items[min(self.picker.index, len(items) - 1)]
        mode = self.mode
        self._dismiss()
        if mode == "commands":
            if complete:
                self.prompt.text = f"/{value.name} "
            else:
                self._submit_command(f"/{value.name}")
        elif mode == "task":
            if self.locked:
                self.message = "Wait for the current run or settings save to finish."
                return
            self.selected_id = value.task_id
            self._spawn(self._persist_cli_state("selection", {"task_id": value.task_id}))
            self._spawn(self._say(f"Selected · {value.title}. Use /settings or /run."))
        elif mode == "history":
            title = self.task.title
            self._spawn(self._say(summary_text(value, title)))

    def _submit_command(self, text):
        self._remember_command(text)
        self.prompt.text = text.strip()
        self.prompt.buffer.reset(append_to_history=True)
        self.mode = ""
        self._spawn(self._dispatch(text))

    async def _dispatch(self, text):
        text = text.strip()
        if not text:
            return
        self.message = ""
        command, argument = parse_command(text)
        if command is None:
            await self._say("Use / to choose a command, or /help for guidance. Task selection: /task.")
            return
        if not self.ready and command.name not in ("help", "quit"):
            await self._say("Runtime is not ready. See the startup message; /help and /quit are available.")
            return
        try:
            await getattr(self, command.handler)(argument)
        except Exception as exc:
            await self._say(f"/{command.name} failed: {redact(exc)}")

    async def _select_task(self, _argument):
        if self.locked:
            await self._say("Wait for the current run or settings save to finish.")
            return
        self.mode = "task"
        self.picker.search.text = ""
        self.picker.index = 0
        self.layout.focus(self.picker.search)

    async def _settings(self, _argument):
        if self.locked:
            await self._say("Settings require an idle task.")
            return
        self.settings_busy = True
        try:
            snapshot = await asyncio.to_thread(runtime_config_snapshot)
            if self.closing:
                return
            task = self.task
            options = self.options[task.task_id]
            if not task.requires_full_inventory:
                options = replace(options, limit=configured_task_limit(snapshot[2], task.task_id))
            options = replace(options, **configured_task_options(snapshot[2], task.task_id))
            self.settings_snapshot = snapshot
            self.settings_form.open(task, options, snapshot[0])
            self.mode = "settings"
            self.layout.focus(self.settings_form.targets()[0])
        finally:
            self.settings_busy = False

    def _save_settings(self):
        if self.settings_busy or not self.settings_form.fields:
            return
        try:
            if self.locked:
                raise ValueError("Settings are locked until the foreground run finishes")
            values = self.settings_form.values()
            options = replace(self.options[self.task.task_id], **values)
        except ValueError as exc:
            self.settings_form.error = str(exc)
            return
        changes = {field.path: values[field.option] for field in self.settings_form.fields}
        self.settings_busy = True
        self._spawn(self._commit_settings(self.task, options, changes, self.settings_snapshot))

    async def _commit_settings(self, task, options, changes, snapshot):
        try:
            await asyncio.to_thread(save_runtime_config, path=snapshot[0], revision=snapshot[1], changes=changes)
        except Exception as exc:
            self.settings_form.error = redact(exc)
        else:
            self.options[task.task_id] = options
            self.settings_busy = False
            self._dismiss()
            await self._say(f"Defaults saved · {task.title}. Changes survive restarts. Use /run to start.")
        finally:
            self.settings_busy = False

    async def _start(self, _argument):
        if self.locked:
            await self._say("Wait for the current run or settings save to finish.")
            return
        await self.lifecycle.start(self.task, self.options[self.task.task_id])

    async def _stop(self, _argument):
        self._request_stop()

    def _request_stop(self):
        self.lifecycle.request_stop()

    async def _history(self, _argument):
        self.mode = "history"
        self.history_rows = []
        self.picker.search.text = ""
        self.picker.index = 0
        self.layout.focus(self.picker.search)
        self._picker_generation += 1
        generation = self._picker_generation
        task_id = self.selected_id
        rows = await asyncio.to_thread(self.db.list_recent_runs_for_task, task_id, limit=config_integer("terminal", "history_rows"))
        if self.mode == "history" and self._picker_generation == generation:
            self.history_rows = rows

    async def _summary(self, _argument):
        presentation_status = None
        if self.lifecycle.foreground:
            title = self.lifecycle.foreground.title
            run_id = self.lifecycle.foreground_run_id
            if run_id is None:
                phase = "Stopping" if self.lifecycle.stopping else "Starting"
                await self._say(f"{phase} · {title} · no run snapshot yet.")
                return
            run = await asyncio.to_thread(self.db.get_run, run_id)
            if run is None:
                raise RuntimeError(f"Run {run_id} is missing; current progress is unavailable")
            failure = await asyncio.to_thread(self.runner.get_run_error, run_id)
            if self.lifecycle.foreground_run_id == run_id and run.get("status") not in TASK_RUN_ACTIVE_STATUSES:
                presentation_status = "finalizing"
        else:
            title = self.task.title
            run = await asyncio.to_thread(self.db.get_latest_run_for_task, self.selected_id)
            failure = None
        await self._say(summary_text(run, title, failure=failure, presentation_status=presentation_status))

    async def _gemini(self, _argument):
        keys = await asyncio.to_thread(load_gemini_keys)
        if not keys:
            await self._say("No Gemini keys configured.")
            return
        models = await asyncio.to_thread(load_required_gemini_model_pool)
        key_ids = tuple(dict.fromkeys(key.key_id for key in keys))
        counts = await asyncio.to_thread(
            self.db.get_gemini_exhaustion_counts, key_ids=key_ids, models=models,
        )
        accounts = len({key.account_id for key in keys})
        lines = [f"Gemini · {len(key_ids)} keys · {accounts} accounts"]
        lines.extend(f"{model}: {counts[model]}/{len(key_ids)} keys exhausted" for model in models)
        await self._say("\n".join(lines))

    async def _reset(self, _argument):
        keys = await asyncio.to_thread(load_gemini_keys)
        models = await asyncio.to_thread(load_required_gemini_model_pool)
        cleared = await asyncio.to_thread(
            self.db.reset_gemini_exhaustion,
            key_ids=tuple(dict.fromkeys(key.key_id for key in keys)), models=models,
        )
        await self._say(f"Gemini exhaustion reset · {cleared} key/model states cleared.")

    async def _help(self, _argument):
        await self._say(help_text())

    async def _quit(self, _argument):
        self._request_exit()

    def _request_interrupt(self):
        if self.closing or self.lifecycle.stopping:
            self._force_exit()
        elif self.lifecycle.foreground:
            self._request_stop()
        elif self.settings_busy:
            self._request_exit()
        elif self.mode == "settings":
            self._dismiss()
        elif self.mode in ("task", "history") and self.picker.search.text:
            self.picker.search.text = ""
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
        if self.lifecycle.foreground:
            self.lifecycle.stopping = True
        self._spawn(self._stop_for_exit())

    async def _stop_for_exit(self):
        if self.runner:
            try:
                await asyncio.to_thread(self.runner.request_shutdown)
            except Exception as exc:
                await self._say("Safe exit recording failed: " + redact(exc) + ". Ctrl-C again forces exit.")

    async def _drain(self):
        try:
            await asyncio.to_thread(self.output.drain)
        except OutputFailure:
            pass  # The consumer already surfaced the failure and requested stop.

    async def _signal_completion(self, run, *, failure=None):
        if not self.display.enable_bell:
            return
        for index in range(completion_bell_count(run, failure=failure)):
            if self.output.failure:
                return
            if index:
                await asyncio.sleep(COMPLETION_BELL_INTERVAL_SECONDS)
            self.display.bell()
            # Keep physical writes on the terminal consumer and space delivered bells.
            while self.display.pending and not self.output.failure:
                await asyncio.sleep(config_number("terminal", "output_poll_seconds", minimum=0.001))

    async def _refresh(self):
        while True:
            if self.ready:
                idle = await self.lifecycle.poll()
                if idle is None:
                    continue
                if self.closing and self.startup_finished and not self.lifecycle.foreground and idle:
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
            await asyncio.sleep(config_number("terminal", "status_poll_seconds", minimum=0.01))

    async def _show_emergency_notice(self, text):
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
                    await asyncio.sleep(config_number("terminal", "output_poll_seconds", minimum=0.001))
            except Exception as exc:
                self.output.fail(exc)
                self.message = self.output.failure
                self._request_exit()
                await self._show_emergency_notice(self.output.failure + ". Safe stop requested; Ctrl-C again forces exit.")
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
        apply_catalog_migrations(settings, write=self.output.write)
        self.db.init_local_state()
        descriptors = self.descriptor_factory()
        self.cli_state = CLIStateStore(settings.local_state_path)
        remembered_id, commands = self.cli_state.load()
        task_ids = {task.task_id for task in descriptors}
        selected_id = self.arguments.task
        if selected_id is None:
            selected_id = remembered_id if remembered_id in task_ids else config_text("terminal", "initial_task")
        selected = next((task for task in descriptors if task.task_id == selected_id), None)
        if selected is None:
            raise ValueError(f"Unknown task ID: {selected_id}")
        if selected.requires_full_inventory and self.arguments.limit is not None:
            raise ValueError(f"{selected.title} requires the complete inventory; omit --limit")
        recovered = self.db.recover_active_runs()
        if recovered:
            self.output.write(f"Recovered interrupted runs: {recovered}\n")
        self.runner = TaskRunner(self.db, descriptors, log_factory=self.output.sink)
        return descriptors, selected_id, commands

    async def _startup(self):
        try:
            self.descriptors, self.selected_id, self._command_history = await asyncio.to_thread(self._initialize)
            self.descriptors.sort(key=lambda item: (item.group, item.title))
            config = await asyncio.to_thread(load_runtime_config)
            for task in self.descriptors:
                extraction = task.task_id == "library.extract_non_pdf"
                task_options = configured_task_options(config, task.task_id)
                saved_limit = configured_task_limit(config, task.task_id)
                if task.requires_full_inventory and saved_limit is not None:
                    raise ValueError(f"{task.title} requires the complete inventory; remove its saved candidate limit")
                source_cohort = task.task_id == self.arguments.task and task.task_id in {
                    "library.extract_non_pdf", "library.generate_book_previews",
                }
                self.options[task.task_id] = RunOptions(
                    limit=None if task.requires_full_inventory else (
                        self.arguments.limit if self.arguments.limit is not None else saved_limit),
                    per_mime_limit=self.arguments.per_mime_limit if extraction else None,
                    retry_known_failures=self.arguments.retry_known_failures if source_cohort else False,
                    only_md5s=tuple(dict.fromkeys(self.arguments.only_md5)) if source_cohort else (),
                    **task_options,
                )
            # Reset the toolkit loader while retaining the draft and commands
            # entered during initialization. History is never auto-submitted.
            buffer = self.prompt.buffer
            buffer.reset(document=buffer.document)
            buffer.history = InMemoryHistory(history_strings=self._command_history + self._startup_commands)
            changed = False
            for text in self._startup_commands:
                changed = append_command(self._command_history, text) or changed
            self._startup_commands.clear()
            self._cli_history_loaded = True
            self._spawn(self._persist_cli_state("selection", {"task_id": self.selected_id}))
            if changed:
                self._spawn(self._persist_cli_state("commands", {"entries": list(self._command_history)}))
            self.ready = True
            self.message = ""
            await self._say(f"Manzara · {self.task.title} selected. /task to change, /settings to edit, /run to start.")
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
            # consumer remains alive while the task/operations finish off-loop.
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
                            await self._show_emergency_notice(self.output.failure)
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
        return 1 if self.output.failure or not self.ready else 0
