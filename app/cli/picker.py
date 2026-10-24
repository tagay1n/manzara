"""Search, selection, and presentation for command, task, and history pickers."""

from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import TextArea

from app.cli.commands import COMMANDS
from app.cli.presentation import elapsed, status
from app.task_runtime.logging import redact


class Picker:
    def __init__(self, *, mode, command_text, descriptors, history_rows, task_title):
        self._mode = mode
        self._command_text = command_text
        self._descriptors = descriptors
        self._history_rows = history_rows
        self._task_title = task_title
        self.index = 0
        self.search = TextArea(height=1, multiline=False, prompt="Search › ")
        self.search.buffer.on_text_changed += self._search_changed
        self.container = ConditionalContainer(HSplit([
            Window(FormattedTextControl(self._title), height=1),
            ConditionalContainer(self.search, filter=Condition(lambda: self._mode() in ("task", "history"))),
            Window(FormattedTextControl(self._text), height=Dimension(min=1, max=6), wrap_lines=True),
        ]), filter=Condition(lambda: self._mode() in ("commands", "task", "history")))

    def _search_changed(self, _buffer):
        self.index = 0

    def items(self):
        mode = self._mode()
        if mode == "commands":
            query = self._command_text().removeprefix("/").lower()
            return [(command, f"/{command.name}  {command.description}")
                    for command in COMMANDS if command.name.startswith(query)]
        query = self.search.text.casefold()
        if mode == "task":
            return [(task, f"{task.title} · {task.group}")
                    for task in self._descriptors()
                    if query in f"{task.title} {task.task_id} {task.group}".casefold()]
        if mode == "history":
            return [(run, f"Run {run['run_id']} · {status(run)} · {elapsed(run)} · {run['started_at']}")
                    for run in self._history_rows()
                    if query in f"{run['run_id']} {status(run)} {run['started_at']}".casefold()]
        return []

    def move(self, step):
        count = len(self.items())
        if count:
            self.index = (self.index + step) % count

    def _title(self):
        titles = {"commands": "Commands", "task": "Tasks · select, then /run",
                  "history": f"Recent runs · {self._task_title()} (latest 20)"}
        title = titles.get(self._mode(), "")
        items = self.items()
        if items:
            title += f" · {min(self.index, len(items) - 1) + 1} of {len(items)}"
        return [("", title)]

    def _text(self):
        items = self.items()
        if not items:
            return [("class:disabled", "No matches")]
        self.index = min(self.index, len(items) - 1)
        rows = []
        for index, (_, label) in enumerate(items):
            if index:
                rows.append(("", "\n"))
            if index == self.index:
                # Keep the selected row visible at viewport edges.
                rows.append(("[SetCursorPosition]", ""))
            style = "class:selected" if index == self.index else ""
            rows.append((style, f"{'›' if index == self.index else ' '} {redact(label)}"))
        return rows
