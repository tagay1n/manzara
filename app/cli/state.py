"""CLI preferences and bounded command recall in the existing local store."""

from pathlib import Path

from app.cli.commands import parse_command
from app.operational_state import OperationalStateStore

COMMAND_HISTORY_LIMIT = 1000
_SCOPE = "cli.session"


def recall_command(text: str) -> str | None:
    command, argument = parse_command(text)
    if command is None:
        return None
    return f"/{command.name}" + (f" {argument}" if argument else "")


def append_command(entries: list[str], text: str) -> bool:
    command = recall_command(text)
    if command is None or (entries and entries[-1] == command):
        return False
    entries.append(command)
    del entries[:-COMMAND_HISTORY_LIMIT]
    return True


class CLIStateStore:
    def __init__(self, path: Path | str):
        self.store = OperationalStateStore(path)

    def load(self) -> tuple[str | None, list[str]]:
        selection = self.store.get(_SCOPE, "selection")
        task_id = None
        if selection is not None:
            if not isinstance(selection, dict) or not isinstance(selection.get("task_id"), str):
                raise ValueError("Invalid saved CLI task selection in local state")
            task_id = selection["task_id"]
        history = self.store.get(_SCOPE, "commands")
        entries = []
        if history is not None:
            if not isinstance(history, dict) or not isinstance(history.get("entries"), list):
                raise ValueError("Invalid saved CLI command history in local state")
            for text in history["entries"]:
                if not isinstance(text, str):
                    raise ValueError("Invalid saved CLI command history entry in local state")
                append_command(entries, text)
        return task_id, entries

    def save(self, item_id: str, payload: dict) -> None:
        self.store.put(_SCOPE, item_id, payload)
