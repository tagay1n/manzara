"""Shared path contracts for guarded document cleanup plans."""

from __future__ import annotations

from pathlib import PurePosixPath


def cleanup_target_path(
    filtered_out_path: str,
    *,
    reason: str,
    source_root_path: str,
    source_path: str,
) -> str:
    """Preserve a document's hierarchy below its configured source root."""
    source = PurePosixPath(str(source_path).removeprefix("disk:"))
    source_root = PurePosixPath(str(source_root_path).removeprefix("disk:"))
    if not source.name or source.name in {".", ".."}:
        raise ValueError("Cleanup source path must identify a file")
    try:
        relative = source.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(
            f"Cleanup source is outside configured document root: {source}"
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Cleanup source path has an unsafe relative hierarchy")
    reason_path = PurePosixPath(str(reason))
    if len(reason_path.parts) != 1 or reason_path.name in {"", ".", ".."}:
        raise ValueError("Cleanup reason must be one safe path segment")
    root = PurePosixPath(str(filtered_out_path).removeprefix("disk:").rstrip("/"))
    if root == source_root or source_root in root.parents:
        raise ValueError("Filtered-out root must be outside the document source root")
    return str(root / reason_path / relative)


__all__ = ["cleanup_target_path"]
