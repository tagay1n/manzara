"""Shared structural-corruption contract for Library source processing."""

from __future__ import annotations

from typing import Any

from app.document_cleanup_paths import cleanup_target_path


class CorruptDocumentError(ValueError):
    """A verified source has deterministic structural damage."""

    def __init__(self, detector: str, message: str) -> None:
        self.detector = str(detector or "document_structure").strip()
        detail = str(message or "Source document is structurally corrupt")
        super().__init__(f"{self.detector}: {detail}")


class PasswordProtectedDocumentError(ValueError):
    """A structurally valid document cannot be read without a password."""


def build_corrupt_cleanup_plan(
    *,
    storage: Any,
    md5: str,
    source_path: str,
    mime_type: str,
    source_size: int,
    task_id: str,
    run_id: int,
    error: CorruptDocumentError,
) -> dict[str, Any]:
    """Build one guarded, hierarchy-preserving corrupt-document move plan."""
    return {
        "scope": "document",
        "action": "move",
        "reason": "corrupted",
        "md5": str(md5),
        "source_resource_id": None,
        "source_path": str(source_path),
        "target_path": cleanup_target_path(
            storage.filtered_out_path,
            reason="corrupted",
            source_root_path=storage.source_path,
            source_path=source_path,
        ),
        "evidence": {
            "detector": error.detector,
            "error": str(error)[:4000],
            "source_size": int(source_size),
            "mime_type": str(mime_type or ""),
            "task_id": str(task_id),
            "run_id": int(run_id),
        },
    }


__all__ = [
    "CorruptDocumentError",
    "PasswordProtectedDocumentError",
    "build_corrupt_cleanup_plan",
]
