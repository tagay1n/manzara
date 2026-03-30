"""Shayan catalog aggregation and per-episode actions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Dict

from app.db import Database


_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9]+")


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _program_slug(category: str, program: str) -> str:
    base = f"{category}-{program}".strip().lower()
    base = _SLUG_CLEAN_RE.sub("-", base).strip("-")
    return base or "program"


def _extract_local_file_path(payload: Dict[str, Any], output_path: Path) -> Path | None:
    raw = _safe_text(payload.get("file"))
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    return (output_path / candidate).resolve()


def _is_uploaded_manifest_row(row: Dict[str, Any]) -> bool:
    status = _safe_text(row.get("yadisk_status")).lower() or "pending"
    remote_path = _safe_text(row.get("yadisk_remote_path"))
    uploaded_hash = _safe_text(row.get("yadisk_uploaded_payload_hash"))
    payload_hash = _safe_text(row.get("manifest_payload_hash"))
    if status != "uploaded":
        return False
    if not remote_path:
        return False
    return bool(uploaded_hash and payload_hash and uploaded_hash == payload_hash)


def _payload_from_catalog_row(row: Dict[str, Any]) -> Dict[str, Any]:
    snapshot_payload = row.get("snapshot_payload")
    manifest_payload = row.get("manifest_payload")
    if isinstance(snapshot_payload, dict) and snapshot_payload:
        return snapshot_payload
    if isinstance(manifest_payload, dict):
        return manifest_payload
    return {}


def build_shayan_catalog(
    db: Database,
    *,
    output_path: Path,
) -> Dict[str, Any]:
    """Build grouped Shayan program/episode catalog payload."""
    rows = db.list_shayan_catalog_rows()
    programs: Dict[tuple[str, str], Dict[str, Any]] = {}
    total_downloaded = 0
    total_uploaded = 0
    total_episodes = 0

    for row in rows:
        entry_key = _safe_text(row.get("entry_key"))
        if not entry_key:
            continue
        payload = _payload_from_catalog_row(row)
        category = _safe_text(payload.get("category")) or "unknown"
        program = _safe_text(payload.get("program")) or "Unknown program"
        season = _safe_int(payload.get("season"))
        episode = _safe_int(payload.get("episode"))
        title = _safe_text(payload.get("title")) or None
        local_file = _extract_local_file_path(payload, output_path)
        downloaded = bool(local_file and local_file.exists())
        uploaded = _is_uploaded_manifest_row(row)

        if downloaded:
            total_downloaded += 1
        if uploaded:
            total_uploaded += 1
        total_episodes += 1

        program_key = (category.lower(), program)
        group = programs.get(program_key)
        if group is None:
            group = {
                "program_id": _program_slug(category, program),
                "category": category,
                "program": program,
                "episodes": [],
            }
            programs[program_key] = group

        group["episodes"].append(
            {
                "entry_key": entry_key,
                "category": category,
                "program": program,
                "season": season,
                "episode": episode,
                "title": title,
                "downloaded": downloaded,
                "uploaded": uploaded,
            }
        )

    program_items = list(programs.values())
    for item in program_items:
        episodes = item["episodes"]
        episodes.sort(
            key=lambda entry: (
                int(entry.get("season") or 999999),
                int(entry.get("episode") or 999999),
                _safe_text(entry.get("title")),
                _safe_text(entry.get("entry_key")),
            )
        )
        item["stats"] = {
            "episodes": len(episodes),
            "downloaded": len([entry for entry in episodes if bool(entry.get("downloaded"))]),
            "uploaded": len([entry for entry in episodes if bool(entry.get("uploaded"))]),
        }

    program_items.sort(key=lambda item: (_safe_text(item.get("category")).lower(), _safe_text(item.get("program")).lower()))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "programs": len(program_items),
            "episodes": total_episodes,
            "downloaded": total_downloaded,
            "uploaded": total_uploaded,
        },
        "programs": program_items,
    }


def request_episode_redownload(
    *,
    db: Database,
    output_path: Path,
    runner: Any,
    entry_key: str,
) -> Dict[str, Any]:
    """Delete one manifest entry + local file and request a download run."""
    normalized_key = _safe_text(entry_key)
    if not normalized_key:
        raise ValueError("entry_key must be non-empty")

    manifest_entry = db.get_shayan_manifest_entry(normalized_key)
    if manifest_entry is None:
        latest_snapshot = db.get_latest_shayan_snapshot_entries()
        if normalized_key not in latest_snapshot:
            raise LookupError("Episode not found")
        payload = latest_snapshot.get(normalized_key) or {}
        manifest_deleted = False
    else:
        payload = manifest_entry.get("payload") if isinstance(manifest_entry.get("payload"), dict) else {}
        manifest_deleted = db.delete_shayan_manifest_entry(normalized_key)

    local_deleted = False
    local_missing = False
    local_file = _extract_local_file_path(payload, output_path)
    if local_file is not None:
        if local_file.exists():
            local_file.unlink()
            local_deleted = True
        else:
            local_missing = True

    download_result = runner.start_task("shayan.download_new")
    db.insert_event(
        "task.artifact",
        task_id="shayan.download_new",
        run_id=None,
        panel_id="shayan",
        payload={
            "kind": "shayan.episode_redownload_requested",
            "entry_key": normalized_key,
        },
    )
    return {
        "entry_key": normalized_key,
        "manifest_deleted": bool(manifest_deleted),
        "local_deleted": local_deleted,
        "local_missing": local_missing,
        "download": download_result,
    }

