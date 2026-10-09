"""One registry for deterministic command dispatch, completion, and help."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    handler: str
    arguments: tuple[str, ...] = ("",)


COMMANDS = (
    Command("task", "Select a task; /task all includes disabled tasks", "_select_task", ("", "all")),
    Command("run", "Start/resume the selected task", "_start"),
    Command("stop", "Stop safely and keep Manzara open", "_stop"),
    Command("settings", "Edit workers and candidate limit", "_settings"),
    Command("history", "Inspect the selected task's recent runs", "_history"),
    Command("summary", "Print the current run or latest saved summary", "_summary"),
    Command("help", "Print commands and keyboard guidance", "_help"),
    Command("quit", "Stop safely, drain output, and exit", "_quit"),
)
COMMAND_BY_NAME = {command.name: command for command in COMMANDS}


def help_text() -> str:
    commands = "\n".join(f"  /{command.name:<10} {command.description}" for command in COMMANDS)
    return ("Manzara commands\n" + commands + "\n\n"
            "  / opens command completion; arrows select, Enter chooses, Tab completes.\n"
            "  Task/history pickers: type to search; Esc dismisses.\n"
            "  Settings: Tab changes focus; Enter/Ctrl-S saves; Esc cancels.\n"
            "  Ctrl-C: stop work and stay; press again while stopping/exiting to force exit.\n"
            "  At idle: Ctrl-C clears input, or exits when input is empty.\n"
            "  New interactive logs live only in terminal scrollback; saved summaries use /history.")
