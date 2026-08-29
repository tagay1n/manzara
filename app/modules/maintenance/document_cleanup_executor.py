"""Guarded Yandex mutation boundary for persisted document cleanup plans."""

from __future__ import annotations

from typing import Any, Mapping


def execute_yandex_cleanup(item: Mapping[str, Any], *, yadisk: Any) -> None:
    """Execute exactly one persisted plan; reject all ad-hoc mutations."""
    cleanup_id = item.get("cleanup_id")
    if not isinstance(cleanup_id, int) or cleanup_id <= 0:
        raise ValueError("Yandex cleanup requires a persisted cleanup_id")
    status = str(item.get("status") or "").strip()
    if status not in {"planned", "running"}:
        raise ValueError("Yandex cleanup status must be planned or running")
    source_path = str(item.get("source_path") or "").strip()
    if not source_path:
        raise ValueError("Yandex cleanup requires source_path")
    action = str(item.get("action") or "").strip()
    if action == "delete":
        yadisk.remove(source_path, permanently=True)
        return
    if action == "move":
        target_path = str(item.get("target_path") or "").strip()
        if not target_path:
            raise ValueError("Move cleanup requires target_path")
        yadisk.move(source_path, target_path, overwrite=True)
        return
    raise ValueError(f"Unsupported cleanup action: {action!r}")


__all__ = ["execute_yandex_cleanup"]
