"""Structured run summary builders."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List


_BACKUP_LABEL_RE = re.compile(r"new backup label\s*=\s*([^\s]+)", re.IGNORECASE)
_BACKUP_SIZE_RE = re.compile(r"\b(?:full|incr)\s+backup size\s*=\s*([^,]+)", re.IGNORECASE)


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def build_default_run_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    """Build fallback summary from run metadata only."""
    status = str(run.get("status") or "unknown")
    error_text = str(run.get("error_text") or "").strip() or None
    message = "Run completed."
    if status == "failed":
        message = error_text or "Run failed."
    elif status == "stopped":
        message = "Run stopped."
    elif status == "running":
        message = "Run is in progress."
    elif status == "starting":
        message = "Run is starting."
    elif status in {"stopping_graceful", "stopping_force"}:
        message = "Run is stopping."

    started_at = run.get("started_at")
    finished_at = run.get("finished_at")
    duration_seconds = None
    started_dt = _parse_iso(started_at)
    finished_dt = _parse_iso(finished_at)
    if started_dt and finished_dt:
        duration_seconds = max(0, int((finished_dt - started_dt).total_seconds()))

    return {
        "kind": "default",
        "status": status,
        "message": message,
        "exit_code": run.get("exit_code"),
        "duration_seconds": duration_seconds,
        "highlights": [],
    }


def build_structured_run_summary(
    *,
    task_id: str,
    panel_id: str,
    status: str,
    exit_code: Any,
    error_text: Any,
    stop_mode: Any,
    started_at: Any,
    finished_at: Any,
    log_lines: List[str],
    artifacts: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build task-aware structured summary object."""
    summary = build_default_run_summary(
        {
            "status": status,
            "exit_code": exit_code,
            "error_text": error_text,
            "started_at": started_at,
            "finished_at": finished_at,
        }
    )
    summary["task_id"] = task_id
    summary["panel_id"] = panel_id
    summary["stop_mode"] = stop_mode
    summary["kind"] = task_id
    if isinstance(artifacts, dict) and artifacts:
        summary["artifacts"] = artifacts

    if task_id.startswith("maintenance.pgbackrest_backup_"):
        summary["kind"] = "maintenance.pgbackrest_backup"
        backup_label = None
        backup_size = None
        s3_verified = False
        for line in log_lines:
            if backup_label is None:
                match = _BACKUP_LABEL_RE.search(line)
                if match:
                    backup_label = match.group(1).strip()
            if backup_size is None:
                match = _BACKUP_SIZE_RE.search(line)
                if match:
                    backup_size = match.group(1).strip()
            if "S3 backup verification passed" in line:
                s3_verified = True
        if backup_label:
            summary["highlights"].append({"label": "Backup Label", "value": backup_label})
        if backup_size:
            summary["highlights"].append({"label": "Backup Size", "value": backup_size})
        if s3_verified:
            summary["highlights"].append({"label": "S3 Verify", "value": "passed"})
        if status == "completed" and backup_label:
            summary["message"] = f"Backup completed: {backup_label}"
        elif status == "completed":
            summary["message"] = "Backup completed."
        return summary

    if task_id == "maintenance.sync_documents_s3":
        sync_artifacts = artifacts if isinstance(artifacts, dict) else {}
        verified = int(sync_artifacts.get("verified") or 0)
        uploaded = int(sync_artifacts.get("uploaded") or 0)
        reuploaded = int(sync_artifacts.get("reuploaded") or 0)
        private_cleaned = int(sync_artifacts.get("private_cleaned") or 0)
        failed = int(sync_artifacts.get("failed") or 0)
        if sync_artifacts.get("kind") == "maintenance.document_s3_sync_summary":
            summary["highlights"].extend(
                [
                    {"label": "Verified", "value": str(verified)},
                    {"label": "Uploaded", "value": str(uploaded)},
                    {"label": "Re-uploaded", "value": str(reuploaded)},
                    {"label": "Private cleaned", "value": str(private_cleaned)},
                    {"label": "Failed", "value": str(failed)},
                ]
            )
            outcome = {
                "completed": "completed",
                "failed": "failed",
                "stopped": "stopped",
            }.get(status, status)
            summary["message"] = (
                f"Document sync {outcome}: {verified} verified, "
                f"{uploaded} uploaded, {failed} failed."
            )
        else:
            summary["message"] = "Document sync completed."
        return summary

    if panel_id == "shayan" and status == "completed":
        if task_id.endswith(".scan_changes"):
            scan_artifacts = artifacts if isinstance(artifacts, dict) else {}
            added = int(scan_artifacts.get("episodes_added") or 0)
            changed = int(scan_artifacts.get("episodes_changed") or 0)
            removed = int(scan_artifacts.get("episodes_removed") or 0)
            if scan_artifacts.get("kind") == "shayan.snapshot_diff":
                summary["highlights"].append({"label": "Added", "value": str(added)})
                summary["highlights"].append({"label": "Changed", "value": str(changed)})
                summary["highlights"].append({"label": "Removed", "value": str(removed)})
                summary["message"] = f"Scan completed: +{added} ~{changed} -{removed}."
            else:
                summary["message"] = "Scan completed."
        elif task_id.endswith(".download_new"):
            download_artifacts = artifacts if isinstance(artifacts, dict) else {}
            downloaded = int(download_artifacts.get("downloaded") or 0)
            failed = int(download_artifacts.get("failed") or 0)
            if download_artifacts.get("kind") == "shayan.download_summary":
                summary["highlights"].append({"label": "Downloaded", "value": str(downloaded)})
                summary["highlights"].append({"label": "Failed", "value": str(failed)})
                summary["message"] = f"Download completed: {downloaded} downloaded, {failed} failed."
            else:
                summary["message"] = "Download completed."
        elif task_id.endswith(".upload_yadisk"):
            upload_artifacts = artifacts if isinstance(artifacts, dict) else {}
            uploaded = int(upload_artifacts.get("uploaded") or 0)
            failed = int(upload_artifacts.get("failed") or 0)
            missing_local = int(upload_artifacts.get("missing_local") or 0)
            deleted_local = int(upload_artifacts.get("deleted_local") or 0)
            hash_mismatch = int(upload_artifacts.get("hash_mismatch") or 0)
            if upload_artifacts.get("kind") == "shayan.upload_yadisk_summary":
                summary["highlights"].append({"label": "Uploaded", "value": str(uploaded)})
                summary["highlights"].append({"label": "Failed", "value": str(failed)})
                summary["highlights"].append({"label": "Missing local", "value": str(missing_local)})
                summary["highlights"].append({"label": "Deleted local", "value": str(deleted_local)})
                summary["highlights"].append({"label": "Hash mismatch", "value": str(hash_mismatch)})
                summary["message"] = f"Upload completed: {uploaded} uploaded, {failed} failed."
            else:
                summary["message"] = "Upload completed."
        elif task_id.endswith(".transfer_yadisk_webdav"):
            transfer_artifacts = artifacts if isinstance(artifacts, dict) else {}
            copied = int(transfer_artifacts.get("copied") or 0)
            reused = int(transfer_artifacts.get("reused") or 0)
            failed = int(transfer_artifacts.get("failed") or 0)
            if transfer_artifacts.get("kind") == "shayan.yadisk_webdav_transfer_summary":
                summary["highlights"].append({"label": "Copied", "value": str(copied)})
                summary["highlights"].append({"label": "Reused", "value": str(reused)})
                summary["highlights"].append({"label": "Failed", "value": str(failed)})
                summary["message"] = (
                    f"Nextcloud copy completed: {copied} copied, {reused} reused, {failed} failed."
                )
            else:
                summary["message"] = "Nextcloud copy completed."
        else:
            summary["message"] = "Task completed."
        return summary

    if task_id == "library.generate_book_previews" and status == "completed":
        preview_artifacts = artifacts if isinstance(artifacts, dict) else {}
        ready = int(preview_artifacts.get("ready") or 0)
        partial = int(preview_artifacts.get("partial") or 0)
        failed = int(preview_artifacts.get("failed") or 0)
        uploaded = int(preview_artifacts.get("uploaded_objects") or 0)
        reused = int(preview_artifacts.get("reused_objects") or 0)
        if preview_artifacts.get("kind") == "library.book_preview_summary":
            summary["highlights"].extend(
                [
                    {"label": "Ready", "value": str(ready)},
                    {"label": "Partial", "value": str(partial)},
                    {"label": "Failed", "value": str(failed)},
                    {"label": "Uploaded", "value": str(uploaded)},
                    {"label": "Reused", "value": str(reused)},
                ]
            )
            summary["message"] = (
                f"Book previews completed: {ready} ready, {partial} partial, {failed} failed."
            )
        else:
            summary["message"] = "Book previews completed."
        return summary

    if panel_id == "library" and status == "completed":
        summary["message"] = "Library task completed."
        return summary

    return summary
