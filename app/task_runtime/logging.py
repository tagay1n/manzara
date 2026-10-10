"""Shared stdout formatting, redaction, and run-scoped routing."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import re
import threading
import sys
from typing import Callable, Iterator, Protocol


class RunLogSink(Protocol):
    """Redacted stdout output with explicit flush/close lifecycle."""

    def __call__(self, message: str, *, level: str = "INFO") -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


LOG_SINK: ContextVar[RunLogSink | None] = ContextVar("manzara_run_log", default=None)
_STDOUT_LOCK = threading.Lock()


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
def bind_run_log(sink: RunLogSink | None) -> Iterator[None]:
    token = LOG_SINK.set(sink)
    try:
        yield
    finally:
        LOG_SINK.reset(token)


def format_log(message: object, *, level: str = "INFO", context: str = "") -> str:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    safe = redact(message).replace("\n", "\n  ")
    prefix = f"{timestamp} | {redact(level)}"
    if context:
        prefix += f" | {context}"
    return f"{prefix} | {safe}\n"


def write_stdout(text: str) -> None:
    with _STDOUT_LOCK:
        sys.stdout.write(text)
        sys.stdout.flush()


def log_message(message: object, *, level: str = "INFO") -> None:
    """Route shared helpers through the current run or directly to stdout."""
    sink = LOG_SINK.get()
    if sink is not None:
        sink(str(message), level=level)
    else:
        write_stdout(format_log(message, level=level))


class RunLog:
    """One formatter for interactive and scheduled stdout logging."""

    def __init__(self, task_id: str, run_id: int, write: Callable[[str], None],
                 flush: Callable[[], None]):
        self._write = write
        self._flush = flush
        self._context = f"run_id={run_id} task_id={task_id}"
        self._lock = threading.Lock()
        self._closed = False

    def __call__(self, message: str, *, level: str = "INFO") -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Run output sink is closed")
            self._write(format_log(message, level=level, context=self._context))

    def flush(self) -> None:
        self._flush()

    def close(self) -> None:
        with self._lock:
            self._closed = True
