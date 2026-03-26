"""Task run artifact collection for structured run summaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Tuple


def capture_pre_run_artifacts(task: Dict[str, Any]) -> Dict[str, Any]:
    """Capture task-specific pre-run state used for post-run diffing."""
    task_id = str(task.get("task_id") or "")
    handler = _PRE_CAPTURE_HANDLERS.get(task_id)
    if handler is None:
        return {}
    return handler(task)


def collect_post_run_artifacts(
    task: Dict[str, Any],
    *,
    status: str,
    pre_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Build task-specific artifact payload for one run."""
    task_id = str(task.get("task_id") or "")
    if status != "completed":
        return {}
    handler = _POST_COLLECT_HANDLERS.get(task_id)
    if handler is None:
        return {}
    return handler(task, pre_state)


def _capture_pre_shayan_scan(task: Dict[str, Any]) -> Dict[str, Any]:
    snapshot_path = _resolve_artifact_path(task, "snapshot_file")
    if snapshot_path is None:
        return {}
    before_entries = _read_snapshot_entries(snapshot_path)
    return {
        "snapshot_file": str(snapshot_path),
        "snapshot_entry_hashes": _entry_hash_map(before_entries),
    }


def _collect_shayan_scan_artifacts(task: Dict[str, Any], pre_state: Dict[str, Any]) -> Dict[str, Any]:
    snapshot_path = _resolve_artifact_path(task, "snapshot_file")
    if snapshot_path is None:
        text = str(pre_state.get("snapshot_file") or "").strip()
        snapshot_path = Path(text).expanduser() if text else None
    if snapshot_path is None:
        return {}

    before_map = pre_state.get("snapshot_entry_hashes")
    if not isinstance(before_map, dict):
        before_map = {}

    after_entries = _read_snapshot_entries(snapshot_path)
    after_map = _entry_hash_map(after_entries)

    before_ids = set(before_map.keys())
    after_ids = set(after_map.keys())
    added = sorted(after_ids - before_ids)
    removed = sorted(before_ids - after_ids)
    changed = sorted(
        key for key in (before_ids & after_ids) if str(before_map.get(key)) != str(after_map.get(key))
    )

    return {
        "kind": "shayan.snapshot_diff",
        "snapshot_file": str(snapshot_path),
        "episodes_before": len(before_map),
        "episodes_after": len(after_map),
        "episodes_added": len(added),
        "episodes_changed": len(changed),
        "episodes_removed": len(removed),
        "added_sample_ids": added[:10],
        "changed_sample_ids": changed[:10],
        "removed_sample_ids": removed[:10],
    }


def _collect_shayan_download_artifacts(task: Dict[str, Any], _pre_state: Dict[str, Any]) -> Dict[str, Any]:
    summary_file = _resolve_artifact_path(task, "summary_file")
    if summary_file is None:
        return {}
    payload = _read_json(summary_file)
    episodes = payload.get("episodes") if isinstance(payload, dict) else None
    if not isinstance(episodes, dict):
        return {}
    downloaded = _safe_int(episodes.get("downloaded"))
    failed = _safe_int(episodes.get("failed"))
    return {
        "kind": "shayan.download_summary",
        "summary_file": str(summary_file),
        "downloaded": downloaded,
        "failed": failed,
    }


def _resolve_artifact_path(task: Dict[str, Any], key: str) -> Path | None:
    command = task.get("command")
    if not isinstance(command, dict):
        return None
    artifacts = command.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    raw = artifacts.get(key)
    text = str(raw or "").strip()
    if not text:
        return None
    return Path(text).expanduser()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _read_snapshot_entries(path: Path) -> Dict[str, Any]:
    payload = _read_json(path)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if isinstance(entries, dict):
        return entries
    if isinstance(payload, dict):
        return payload
    return {}


def _entry_hash_map(entries: Dict[str, Any]) -> Dict[str, str]:
    mapped: Dict[str, str] = {}
    for key, value in entries.items():
        entry_id, digest = _entry_digest(key, value)
        if entry_id:
            mapped[entry_id] = digest
    return mapped


def _entry_digest(raw_key: Any, raw_value: Any) -> Tuple[str, str]:
    key = str(raw_key or "").strip()
    normalized = json.dumps(raw_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return key, digest


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


_PRE_CAPTURE_HANDLERS = {
    "shayan.scan_changes": _capture_pre_shayan_scan,
}

_POST_COLLECT_HANDLERS = {
    "shayan.scan_changes": _collect_shayan_scan_artifacts,
    "shayan.download_new": _collect_shayan_download_artifacts,
}
