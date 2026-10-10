"""Nonblocking POSIX terminal output shared by the renderer and transcript."""

from __future__ import annotations

import asyncio
import os
import sys

from prompt_toolkit.application import Application
from prompt_toolkit.output.vt100 import Vt100_Output

from app.cli.output import OutputFailure
from app.runtime_config import config_integer
from app.task_runtime.logging import redact


def _open_terminal(stream) -> int:
    # Reopen the terminal, not dup(): dup shares file-status flags with stdout.
    # All actual writes use this independent nonblocking descriptor.
    return os.open(os.ttyname(stream.fileno()), os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)


def emergency_notice(text: str) -> None:
    """Best effort only: diagnostics must not wait on stdout or its Python lock."""
    try:
        fd = _open_terminal(sys.stdout)
        try:
            data = (redact(text) + "\n").encode(sys.stdout.encoding or "utf-8", "replace")
            os.write(fd, data[:4096])
        finally:
            os.close(fd)
    except (OSError, ValueError):
        pass


class TerminalDisplay(Vt100_Output):
    """Toolkit flushes stage bytes; only drain() writes them to the terminal."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending = bytearray()
        self._fd: int | None = None
        self.failure: str | None = None

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    def flush(self) -> None:
        # Renderer flushes are synchronous. Never perform terminal I/O here.
        text = "".join(self._buffer)
        self._buffer.clear()
        if self.failure or not text:
            return
        data = text.encode(self.encoding() or "utf-8", "replace")
        # Redraws are coalesced while a frame is pending. This additional cap
        # also bounds exceptional renderer output, such as an enormous paste.
        if len(self._pending) + len(data) > config_integer("terminal", "render_buffer_bytes"):
            self.failure = "Terminal rendering exceeded the output buffer limit"
            self._pending.clear()
            return
        self._pending.extend(data)

    async def drain(self) -> None:
        try:
            if self.failure:
                raise OutputFailure(self.failure)
            if self._fd is None:
                self._fd = _open_terminal(self.stdout)
            while self._pending:
                try:
                    written = os.write(self._fd, self._pending[:4096])
                except BlockingIOError:
                    await self._writable()
                    continue
                if written == 0:
                    raise OSError("Terminal accepted no output bytes")
                del self._pending[:written]
                # Large frames must not monopolize the event loop even when
                # the terminal accepts every write immediately.
                await asyncio.sleep(0)
        except (OSError, OutputFailure) as exc:
            self.failure = self.failure or redact(exc)
            self._pending.clear()
            raise OutputFailure(self.failure) from exc

    async def _writable(self) -> None:
        loop = asyncio.get_running_loop()
        ready = loop.create_future()

        def wake():
            if not ready.done():
                ready.set_result(None)

        loop.add_writer(self._fd, wake)
        try:
            await ready
        finally:
            loop.remove_writer(self._fd)

    def emergency_restore(self) -> None:
        """One nonblocking attempt; never drain pending frames on force exit."""
        if self._fd is not None:
            try:
                os.write(self._fd, b"\x1b[0m\x1b[?25h\x1b[?2004l\x1b[?7h\x1b[0 q")
            except OSError:
                pass

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class InlineApplication(Application):
    """Coalesce UI redraws under backpressure without detaching raw input.

    These overrides use prompt_toolkit 3.0.53's redraw/resize hooks. Compose a
    whole erase/transcript/redraw frame in memory before any asynchronous I/O.
    """

    def __init__(self, *, output: TerminalDisplay, **kwargs):
        self.display = output
        self._redraw_pending = False
        self._resize_pending = False
        self._composing = False
        super().__init__(output=output, **kwargs)

    def _redraw(self, render_as_done: bool = False) -> None:
        if self.display.pending and not self._composing and not render_as_done:
            self._redraw_pending = True
            return
        self._redraw_pending = False
        super()._redraw(render_as_done=render_as_done)

    def _on_resize(self) -> None:
        if self.display.pending:
            self._resize_pending = True
            return
        self._resize_pending = False
        self._composing = True
        try:
            super()._on_resize()
        finally:
            self._composing = False

    def resume_rendering(self) -> None:
        if not self.is_running or self.display.pending:
            return
        if self._resize_pending:
            self._on_resize()
        elif self._redraw_pending:
            self._redraw()

    def print_transcript(self, text: str) -> None:
        # Caller has drained the preceding frame. No await, cooked-mode window,
        # or physical terminal write occurs while composing this frame.
        self._composing = True
        try:
            if self.is_running:
                self.renderer.erase()
            self.output.write(text)
            self.output.flush()
            if self.is_running:
                self._request_absolute_cursor_position()
                self._redraw()
        finally:
            self._composing = False
