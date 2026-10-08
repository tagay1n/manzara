"""Transport-independent task registration and execution contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
from typing import Any, Callable


@dataclass(frozen=True)
class RunOptions:
    workers: int = 1
    limit: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.workers, bool) or not isinstance(self.workers, int) or self.workers < 1:
            raise ValueError("workers must be a positive integer")
        if self.limit is not None and (
            isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1
        ):
            raise ValueError("limit must be a positive integer or unset")

    def as_dict(self) -> dict[str, Any]:
        return {"workers": self.workers, "limit": self.limit}


@dataclass
class RunContext:
    db: Any
    task_id: str
    panel_id: str
    run_id: int
    options: RunOptions
    stop_event: Event
    log: Callable[[str], None]
    progress: Callable[..., None]
    artifact: Callable[[dict[str, Any]], None]

    def should_stop(self) -> bool:
        return self.stop_event.is_set()


@dataclass(frozen=True)
class TaskDescriptor:
    task_id: str
    title: str
    group: str
    definition: dict[str, Any] = field(repr=False)
    execute: Callable[[RunContext], dict[str, Any]] | None = field(default=None, repr=False)
    unavailable_reason: str = "Catalog adaptation and CLI execution are pending."

    @property
    def available(self) -> bool:
        return self.execute is not None
