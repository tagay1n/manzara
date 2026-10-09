"""Run-scoped logs with shared redaction and explicit worker context."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
import re
import threading
from typing import Callable, Iterator, Protocol

from app.run_log_store import run_log_path

LOG_SINK: ContextVar[Callable[[str], None] | None] = ContextVar("manzara_run_log", default=None)


class RunLogSink(Protocol):
    """Run-scoped output; terminal sinks deliberately have no file path."""

    path: Path | None

    def __call__(self, message: str, *, level: str = "INFO") -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


_LOG_REDACTION_PATTERNS = (
    (
        re.compile(
            r"(?i)(\bauthorization\b\s*:\s*(?:bearer|basic)\s+)([^\s,;]+)"
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"(?i)(\b(?:aws_secret_access_key|aws_access_key_id)\b\s*[=:]\s*)([^\s,;]+)"
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"(?i)(\b(?:password|passwd|token|access_token|refresh_token|secret|api[_-]?key)\b\s*[=:]\s*)([^\s,;]+)"
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r'(?i)("?(?:password|passwd|token|access_token|refresh_token|secret|api[_-]?key|aws_secret_access_key|aws_access_key_id)"?\s*:\s*")([^"]+)(")'
        ),
        r"\1<redacted>\3",
    ),
    (
        re.compile(
            r'(?i)("authorization"\s*:\s*"(?:bearer|basic)\s+)([^"]+)(")'
        ),
        r"\1<redacted>\3",
    ),
    (
        re.compile(
            r"(?i)([?&](?:access_token|refresh_token|token|api[_-]?key|password|passwd|secret)=)([^&#\s]+)"
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"(?i)(://[^:/\s]+:)([^@/\s]+)(@)"
        ),
        r"\1<redacted>\3",
    ),
)


def redact(message: object) -> str:
    value = str(message)
    for pattern, replacement in _LOG_REDACTION_PATTERNS:
        value = pattern.sub(replacement, value)
    # Do not allow terminal escape sequences in provider or source text.
    return "".join(char for char in value if char in "\n\t" or (ord(char) >= 32 and not 127 <= ord(char) <= 159))


@contextmanager
def bind_run_log(sink: Callable[[str], None] | None) -> Iterator[None]:
    token = LOG_SINK.set(sink)
    try:
        yield
    finally:
        LOG_SINK.reset(token)


class StdoutRunLog:
    """Workflow log sink: structured, redacted, flushed, and never captured."""

    path = None

    def __init__(self, task_id, panel_id, run_id, console_sink):
        self._console_sink = console_sink
        self._lock = threading.Lock()
        self._context = f"run_id={run_id} task_id={task_id} source=runtime"

    def __call__(self, message, *, level="INFO"):
        timestamp = datetime.now(timezone.utc).isoformat()
        safe = redact(message).replace("\r", "").replace("\n", "\\n")
        with self._lock:
            self._console_sink(f"{timestamp} | {redact(level)} | {self._context} | {safe}")

    def flush(self):
        # The console callback flushes synchronously on every line.
        pass

    def close(self):
        pass


class RunLog:
    def __init__(self, root: Path, task_id: str, panel_id: str, run_id: int,
                 console_sink: Callable[[str], None] | None = None):
        self.path = run_log_path(root, task_id, run_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self._lock = threading.Lock()
        self._console_sink = console_sink
        self._context = f"run_id={run_id} task_id={task_id} panel_id={panel_id} source=runtime"

    def __call__(self, message: str, *, level: str = "INFO") -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        safe = redact(message).replace("\r", "").replace("\n", "\\n")
        line = f"{timestamp} | {level} | {self._context} | {safe}"
        with self._lock:
            self._handle.write(line + "\n")
            self._handle.flush()
            if self._console_sink is not None:
                self._console_sink(line)

    def close(self) -> None:
        with self._lock:
            self._handle.close()

    def flush(self) -> None:
        with self._lock:
            self._handle.flush()
