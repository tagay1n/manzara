"""Transport-independent task registration and execution contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
import re
from typing import Any, Callable

from app.task_runtime.logging import RunLogSink


@dataclass(frozen=True)
class RunOptions:
    limit: int | None = None
    per_mime_limit: int | None = None
    retry_known_failures: bool = False
    only_md5s: tuple[str, ...] = ()
    metadata_response_timeout_seconds: int | None = None
    metadata_first_pages: int | None = None
    metadata_last_pages: int | None = None

    def __post_init__(self) -> None:
        if self.limit is not None and (
            isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1
        ):
            raise ValueError("limit must be a positive integer or unset")
        if self.per_mime_limit is not None and (
            isinstance(self.per_mime_limit, bool) or not isinstance(self.per_mime_limit, int) or self.per_mime_limit < 1
        ):
            raise ValueError("per_mime_limit must be a positive integer or unset")
        if not isinstance(self.retry_known_failures, bool):
            raise ValueError("retry_known_failures must be a boolean")
        if not isinstance(self.only_md5s, tuple) or any(
            not isinstance(md5, str) or re.fullmatch(r"[0-9a-f]{32}", md5) is None for md5 in self.only_md5s
        ):
            raise ValueError("only_md5s must be a tuple of lowercase source MD5s")
        if self.metadata_response_timeout_seconds is not None and (
            type(self.metadata_response_timeout_seconds) is not int
            or not 1 <= self.metadata_response_timeout_seconds <= 900
        ):
            raise ValueError("Metadata response timeout must be an integer from 1 to 900 seconds")
        for name in ("metadata_first_pages", "metadata_last_pages"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.metadata_first_pages == 0 and self.metadata_last_pages == 0:
            raise ValueError("Select at least one first or last page")

    def as_dict(self) -> dict[str, Any]:
        values = {"limit": self.limit}
        if self.per_mime_limit is not None or self.retry_known_failures or self.only_md5s:
            values.update(per_mime_limit=self.per_mime_limit, retry_known_failures=self.retry_known_failures,
                          only_md5s=list(self.only_md5s))
        for name in ("metadata_response_timeout_seconds", "metadata_first_pages", "metadata_last_pages"):
            if getattr(self, name) is not None:
                values[name] = getattr(self, name)
        return values


@dataclass
class RunContext:
    db: Any
    task_id: str
    panel_id: str
    run_id: int
    options: RunOptions
    stop_event: Event
    log: RunLogSink
    progress: Callable[..., None]
    artifact: Callable[[dict[str, Any]], None]

    def should_stop(self) -> bool:
        return self.stop_event.is_set()


@dataclass(frozen=True)
class TaskRegistration:
    """Code-owned metadata and a handler resolved by the composition boundary."""

    task_id: str
    title: str
    group: str
    group_id: str
    handler_module: str
    requires_full_inventory: bool = False
    interactive: bool = True


@dataclass(frozen=True)
class TaskDescriptor:
    task_id: str
    title: str
    group: str
    group_id: str
    execute: Callable[[RunContext], dict[str, Any]] = field(repr=False)
    requires_full_inventory: bool = False
