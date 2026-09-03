"""Bounded readers for append-only task artifact logs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_LOG_LINE_RE = re.compile(
    r"^(?P<ts>[^|]+?)\s+\|\s+(?P<level>[A-Z]+)\s+\|\s+"
    r"run_id=(?P<run_id>\d+)\s+task_id=(?P<task_id>\S+)\s+"
    r"panel_id=(?P<panel_id>\S+)\s+source=(?P<source>\S+)\s+\|\s+"
    r"(?P<message>.*)$"
)
_READ_BLOCK_BYTES = 64 * 1024


def safe_task_slug(value: str) -> str:
    """Return the filesystem-safe task directory name used by the runtime."""
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    return re.sub(r"[^a-z0-9._-]+", "_", text)


def run_log_path(root: Path, task_id: str, run_id: int) -> Path:
    """Resolve one run log below the configured task-runs root."""
    resolved_run_id = int(run_id)
    if resolved_run_id <= 0:
        raise ValueError("run_id must be positive")
    return Path(root) / safe_task_slug(task_id) / f"run-{resolved_run_id}.log"


def _payload(raw: bytes, *, offset: int, fallback_run_id: int) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
    match = _LOG_LINE_RE.match(text)
    if not match:
        return {
            "log_id": int(offset) + 1,
            "run_id": int(fallback_run_id),
            "ts": "",
            "stream": "stdout",
            "line": text,
        }
    values = match.groupdict()
    source = str(values["source"])
    return {
        "log_id": int(offset) + 1,
        "run_id": int(values["run_id"]),
        "ts": str(values["ts"]).strip(),
        "stream": source if source in {"stdout", "stderr"} else "stdout",
        "line": str(values["message"]),
    }


def _read_forward(
    path: Path,
    *,
    run_id: int,
    after_log_id: int,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        if after_log_id > 0:
            handle.seek(max(0, int(after_log_id) - 1))
            handle.readline()
        while len(rows) < limit:
            offset = handle.tell()
            raw = handle.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            rows.append(_payload(raw, offset=offset, fallback_run_id=run_id))
    return rows


def _read_before(
    path: Path,
    *,
    run_id: int,
    end_offset: int,
    limit: int,
) -> list[dict[str, Any]]:
    file_size = path.stat().st_size
    end = max(0, min(int(end_offset), int(file_size)))
    cursor = end
    chunks: list[bytes] = []
    newline_count = 0
    while cursor > 0 and newline_count <= limit:
        start = max(0, cursor - _READ_BLOCK_BYTES)
        with path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read(cursor - start)
        chunks.insert(0, chunk)
        newline_count += chunk.count(b"\n")
        cursor = start

    data = b"".join(chunks)
    base_offset = cursor
    if base_offset > 0:
        boundary = data.find(b"\n")
        if boundary < 0:
            return []
        data = data[boundary + 1 :]
        base_offset += boundary + 1

    segments = data.splitlines(keepends=True)
    complete: list[tuple[int, bytes]] = []
    offset = base_offset
    for segment in segments:
        if segment.endswith(b"\n"):
            complete.append((offset, segment))
        offset += len(segment)
    return [
        _payload(raw, offset=line_offset, fallback_run_id=run_id)
        for line_offset, raw in complete[-limit:]
    ]


def read_run_log(
    root: Path,
    task_id: str,
    run_id: int,
    *,
    after_log_id: int = 0,
    before_log_id: int | None = None,
    tail: bool = False,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """Read one bounded page using run-local byte-offset cursors."""
    resolved_limit = max(1, min(int(limit), 5000))
    path = run_log_path(root, task_id, run_id)
    if not path.is_file():
        return []
    if tail:
        return _read_before(
            path,
            run_id=int(run_id),
            end_offset=path.stat().st_size,
            limit=resolved_limit,
        )
    if before_log_id is not None and int(before_log_id) > 0:
        return _read_before(
            path,
            run_id=int(run_id),
            end_offset=int(before_log_id) - 1,
            limit=resolved_limit,
        )
    return _read_forward(
        path,
        run_id=int(run_id),
        after_log_id=max(0, int(after_log_id)),
        limit=resolved_limit,
    )


def has_run_log_before(root: Path, task_id: str, run_id: int, log_id: int) -> bool:
    """Return whether a cursor has at least one complete predecessor line."""
    path = run_log_path(root, task_id, run_id)
    if not path.is_file() or int(log_id) <= 1:
        return False
    with path.open("rb") as handle:
        prefix_end = max(0, int(log_id) - 1)
        handle.seek(0)
        return b"\n" in handle.read(min(prefix_end, _READ_BLOCK_BYTES)) or prefix_end > _READ_BLOCK_BYTES


__all__ = [
    "has_run_log_before",
    "read_run_log",
    "run_log_path",
    "safe_task_slug",
]
