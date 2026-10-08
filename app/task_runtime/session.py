"""Exclusive ownership of one machine-local orchestration store."""

from __future__ import annotations

import fcntl
from pathlib import Path


class SessionLock:
    def __init__(self, state_path: Path):
        self.path = Path(state_path).expanduser().resolve().with_suffix(".cli.lock")
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.path.open("a", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("Another Manzara CLI owns this local runtime store. Close it first.") from None
        self._handle = handle

    def close(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
