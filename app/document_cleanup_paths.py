"""Shared path contracts for guarded document cleanup plans."""

from __future__ import annotations

from pathlib import PurePosixPath
import re


def source_path(value: str) -> str:
    """Validate an absolute Yandex file/directory path without traversal."""
    if not isinstance(value, str):
        raise ValueError("Yandex path must be text")
    path = value.removeprefix("disk:").rstrip("/")
    if not path.startswith("/") or any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("Yandex path must be absolute and contain no traversal")
    if any(ord(char) < 32 for char in path) or "//" in path:
        raise ValueError("Yandex path has unsafe characters or empty segments")
    return path


def newline_repair_target(value: str) -> str:
    """Change only filename line breaks; directory paths retain strict validation."""
    if not isinstance(value, str):
        raise ValueError('Yandex repair source must be text')
    path = value.removeprefix('disk:')
    parent, separator, name = path.rpartition('/')
    if not separator or not any(char in name for char in '\r\n'):
        raise ValueError('Filename repair requires embedded newlines')
    parent = source_path(parent)
    if any(ord(char) < 32 and char not in '\r\n' for char in name):
        raise ValueError('Filename repair cannot remove other control characters')
    normalized = re.sub(r' *[\r\n]+ *', ' ', name)
    return source_path(parent + '/' + normalized)


def cleanup_source_path(item) -> str:
    """A reviewed filename repair may address its original newline-bearing name."""
    if item.get('reason') != 'filename_newlines':
        return source_path(item.get('source_path'))
    evidence = item.get('evidence_json') or {}
    if (item.get('scope') != 'source_resource' or item.get('action') != 'move'
            or evidence.get('owner_approved') is not True):
        raise ValueError('Filename repair requires an owner-approved resource move')
    original = item.get('source_path')
    target = newline_repair_target(original)
    if target != source_path(item.get('target_path')):
        raise ValueError('Filename repair target differs from newline normalization')
    return original.removeprefix('disk:')


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


__all__ = ["cleanup_source_path", "cleanup_target_path", "newline_repair_target", "source_path"]
