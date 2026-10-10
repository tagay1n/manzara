"""Bounded, redacted worker output with backpressure and one terminal consumer."""

from __future__ import annotations

from collections import deque
import threading

from app.task_runtime.logging import RunLog, redact


class OutputFailure(RuntimeError):
    pass


class TerminalOutput:
    """Bound both entry count and entry size; never silently drop normal output."""

    def __init__(self):
        self._condition = threading.Condition()
        self._writers = threading.Lock()
        self._queue: deque[str] = deque()
        self._in_flight = False
        self._failure: str | None = None

    @property
    def failure(self) -> str | None:
        with self._condition:
            return self._failure

    def write(self, text: str) -> None:
        safe = redact(text)
        # Serialize entire messages, even when a long message spans many chunks.
        with self._writers:
            for offset in range(0, len(safe), 4096):
                with self._condition:
                    while len(self._queue) >= 256 and self._failure is None:
                        self._condition.wait()
                    self._raise_if_failed()
                    chunk = safe[offset:offset + 4096]
                    # Every print batch ends at a line boundary so redrawing
                    # the prompt cannot overwrite a partial transcript line.
                    self._queue.append(chunk if chunk.endswith("\n") else chunk + "\n")
                    self._condition.notify_all()

    def _raise_if_failed(self) -> None:
        if self._failure is not None:
            raise OutputFailure(self._failure)

    def take_batch(self) -> str:
        with self._condition:
            # Repaint live controls between bounded transcript chunks, rather
            # than hiding them behind up to 128 KiB of output at a time.
            chunk = self._queue.popleft() if self._queue else ""
            self._in_flight = bool(chunk)
            self._condition.notify_all()
            return chunk

    def acknowledge(self) -> None:
        with self._condition:
            self._in_flight = False
            self._condition.notify_all()

    def drain(self) -> None:
        with self._condition:
            while (self._queue or self._in_flight) and self._failure is None:
                self._condition.wait()
            self._raise_if_failed()

    def fail(self, error: object) -> None:
        with self._condition:
            self._failure = self._failure or f"Terminal output failed: {redact(error)}"
            self._condition.notify_all()

    def sink(self, task_id: str, _panel_id: str, run_id: int) -> RunLog:
        return RunLog(task_id, run_id, self.write, self.check_failure)

    def check_failure(self) -> None:
        # Delivery belongs to the UI consumer; completion drains separately.
        with self._condition:
            self._raise_if_failed()
