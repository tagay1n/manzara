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
        if sync_artifacts.get("kind") == "maintenance.document_s3_upload_summary":
            pending_before = int(sync_artifacts.get("pending_before") or 0)
            uploaded = int(sync_artifacts.get("uploaded") or 0)
            recovered = int(sync_artifacts.get("recovered_existing") or 0)
            source_cache = int(sync_artifacts.get("source_cache") or 0)
            source_yandex = int(sync_artifacts.get("source_yandex") or 0)
            skipped = int(sync_artifacts.get("skipped_download") or 0)
            stale_cleaned = int(sync_artifacts.get("stale_upload_cleaned") or 0)
            failed = int(sync_artifacts.get("failed") or 0)
            pending_after = int(sync_artifacts.get("pending_after") or 0)
            raw_checkpointed = sync_artifacts.get("checkpointed")
            checkpointed = (
                int(raw_checkpointed)
                if raw_checkpointed is not None
                else uploaded + recovered
            )
            summary["highlights"].extend(
                [
                    {"label": "Pending before", "value": str(pending_before)},
                    {"label": "Uploaded", "value": str(uploaded)},
                    {"label": "Recovered", "value": str(recovered)},
                    {"label": "From cache", "value": str(source_cache)},
                    {"label": "From Yandex", "value": str(source_yandex)},
                    {"label": "Skipped", "value": str(skipped)},
                ]
            )
            if stale_cleaned:
                summary["highlights"].append(
                    {"label": "Stale uploads cleaned", "value": str(stale_cleaned)}
                )
            summary["highlights"].extend(
                [
                    {"label": "Failed", "value": str(failed)},
                    {"label": "Pending after", "value": str(pending_after)},
                ]
            )
            if status == "failed":
                summary["message"] = "Backblaze upload failed before the queue completed."
            elif sync_artifacts.get("stopped"):
                summary["message"] = (
                    f"Backblaze upload stopped safely with {pending_after} still pending."
                )
            else:
                summary["message"] = (
                    f"Backblaze upload completed: {checkpointed} checkpointed, "
                    f"{skipped} skipped, {failed} failed, {pending_after} still pending."
                )
            return summary
        source_files = int(sync_artifacts.get("source_files") or 0)
        source_documents = int(sync_artifacts.get("source_documents") or 0)
        database_rows = int(sync_artifacts.get("database_rows_after") or 0)
        synced = int(sync_artifacts.get("synced_source_documents") or 0)
        unsynced = int(sync_artifacts.get("unsynced_source_documents") or 0)
        discovery_complete = bool(sync_artifacts.get("discovery_complete", True))
        database_only_value = sync_artifacts.get("database_only_rows")
        database_only = (
            int(database_only_value or 0) if discovery_complete else None
        )
        duplicate_paths = int(sync_artifacts.get("duplicates") or 0)
        checkpoint_reused = int(sync_artifacts.get("checkpoint_reused") or 0)
        uploaded = int(sync_artifacts.get("uploaded") or 0)
        updated = int(sync_artifacts.get("updated") or 0)
        discovery_failed = int(sync_artifacts.get("discovery_failed") or 0)
        failed = int(sync_artifacts.get("failed") or 0)
        if sync_artifacts.get("kind") == "maintenance.document_s3_sync_summary":
            fully_synced = bool(sync_artifacts.get("fully_synced"))
            summary["highlights"].extend(
                [
                    {"label": "Source files", "value": str(source_files)},
                    {"label": "Source documents", "value": str(source_documents)},
                    {"label": "Database rows", "value": str(database_rows)},
                    {"label": "Synced", "value": str(synced)},
                    {"label": "Unsynced", "value": str(unsynced)},
                    {
                        "label": "Database-only",
                        "value": (
                            str(database_only)
                            if database_only is not None
                            else "Not evaluated"
                        ),
                    },
                    {"label": "Duplicate paths", "value": str(duplicate_paths)},
                    *(
                        [{"label": "DB checkpoints", "value": str(checkpoint_reused)}]
                        if checkpoint_reused
                        else []
                    ),
                    {"label": "Uploaded", "value": str(uploaded)},
                    {"label": "DB updated", "value": str(updated)},
                    {"label": "Failed", "value": str(failed)},
                    {
                        "label": "Result",
                        "value": (
                            "Fully synced"
                            if fully_synced
                            else "Differences"
                            if discovery_complete
                            else "Discovery incomplete"
                        ),
                    },
                ]
            )
            if discovery_failed:
                summary["highlights"].insert(
                    -1,
                    {"label": "Discovery failures", "value": str(discovery_failed)},
                )
            if status == "failed":
                summary["message"] = "Document sync failed before reconciliation completed."
            elif not discovery_complete:
                if discovery_failed:
                    summary["message"] = (
                        "Document discovery completed with "
                        f"{discovery_failed} unavailable Yandex paths; "
                        "full reconciliation was not evaluated."
                    )
                else:
                    summary["message"] = (
                        f"Document sync stopped after {source_files} discovered files; "
                        "full reconciliation was not evaluated."
                    )
            elif fully_synced:
                summary["message"] = (
                    f"Document reconciliation complete: {synced} source documents "
                    f"and {database_rows} database rows synchronized."
                )
            else:
                summary["message"] = (
                    "Document reconciliation completed with differences: "
                    f"{synced}/{source_documents} source documents synced, "
                    f"{database_only} database-only, {failed} failed."
                )
        else:
            summary["message"] = "Document sync completed."
        return summary

    if task_id == "maintenance.monocorpus_sync":
        data = artifacts if isinstance(artifacts, dict) else {}
        if data.get("kind") == "maintenance.monocorpus_sync_summary":
            summary["highlights"].extend(
                [
                    {"label": "Discovered", "value": str(int(data.get("discovered") or 0))},
                    {"label": "Added", "value": str(int(data.get("created") or 0))},
                    {"label": "Published", "value": str(int(data.get("published") or 0))},
                    {"label": "Cleaned", "value": str(int(data.get("cleanups_completed") or 0))},
                    {"label": "Failed", "value": str(int(data.get("failed") or 0))},
                ]
            )
            summary["message"] = (
                "Monocorpus sync stopped at a safe item boundary."
                if data.get("stopped")
                else "Monocorpus catalog and cleanup queue synchronized."
            )
        return summary

    if task_id == "library.prepare_document_cleanup":
        data = artifacts if isinstance(artifacts, dict) else {}
        if data.get("kind") == "library.document_cleanup_preparation_summary":
            planned_moves = data.get("planned_moves")
            if not isinstance(planned_moves, dict):
                planned_moves = {}
            summary["highlights"].extend(
                [
                    {"label": "Scanned", "value": str(int(data.get("scanned") or 0))},
                    {
                        "label": "By ISBN",
                        "value": str(int(planned_moves.get("by_isbn") or 0)),
                    },
                    {
                        "label": "By language",
                        "value": str(int(planned_moves.get("by_language") or 0)),
                    },
                    {
                        "label": "Non-document",
                        "value": str(
                            int(planned_moves.get("by_non_document_format") or 0)
                        ),
                    },
                    {
                        "label": "ISBN reviews",
                        "value": str(int(data.get("isbn_review_groups") or 0)),
                    },
                ]
            )
            summary["message"] = (
                "Cleanup plan prepared by ISBN, language, and document format; "
                "no storage was mutated."
            )
        return summary

    if task_id == "library.metadata_extract":
        data = artifacts if isinstance(artifacts, dict) else {}
        if data.get("kind") == "library.metadata_extraction_summary":
            values = {
                "Eligible": int(data.get("eligible") or 0),
                "Processed": int(data.get("processed") or 0),
                "Succeeded": int(data.get("succeeded") or 0),
                "Terminal": int(data.get("terminal") or 0),
                "Quota deferred": int(data.get("quota_deferred") or 0),
                "Service deferred": int(data.get("service_deferred") or 0),
                "Source deferred": int(data.get("source_deferred") or 0),
                "Remaining": int(data.get("remaining") or 0),
            }
            summary["highlights"].extend(
                {"label": label, "value": str(value)}
                for label, value in values.items()
            )
            outcome = str(data.get("outcome") or "")
            remaining = values["Remaining"]
            if outcome == "all_keys_exhausted":
                summary["message"] = (
                    f"Paused by Gemini quota with {remaining} documents remaining."
                )
            elif outcome == "stopped":
                summary["message"] = (
                    f"Metadata extraction stopped with {remaining} documents remaining."
                )
            elif remaining:
                summary["message"] = (
                    f"Metadata extraction completed with {remaining} deferred documents."
                )
            else:
                summary["message"] = "Metadata extraction completed."
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
            reused = int(upload_artifacts.get("reused") or 0)
            if upload_artifacts.get("kind") in {
                "shayan.upload_yadisk_summary",
                "shayan.webdav_upload_summary",
            }:
                summary["highlights"].append({"label": "Uploaded", "value": str(uploaded)})
                if upload_artifacts.get("kind") == "shayan.webdav_upload_summary":
                    summary["highlights"].append({"label": "Reused", "value": str(reused)})
                summary["highlights"].append({"label": "Failed", "value": str(failed)})
                summary["highlights"].append({"label": "Missing local", "value": str(missing_local)})
                summary["highlights"].append({"label": "Deleted local", "value": str(deleted_local)})
                if upload_artifacts.get("kind") == "shayan.upload_yadisk_summary":
                    summary["highlights"].append({"label": "Hash mismatch", "value": str(hash_mismatch)})
                summary["message"] = f"Upload completed: {uploaded} uploaded, {failed} failed."
            else:
                summary["message"] = "Upload completed."
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

    if task_id == "library.extract_non_pdf" and status == "completed":
        content_artifacts = artifacts if isinstance(artifacts, dict) else {}
        ready = int(content_artifacts.get("ready") or 0)
        unsupported = int(content_artifacts.get("unsupported") or 0)
        deferred = int(content_artifacts.get("deferred") or 0)
        failed = int(content_artifacts.get("failed") or 0)
        images = int(content_artifacts.get("uploaded_images") or 0)
        stale_images = int(content_artifacts.get("deleted_stale_images") or 0)
        per_mime_limit = content_artifacts.get("per_mime_limit")
        if content_artifacts.get("kind") == "library.non_pdf_extraction_summary":
            summary["highlights"].extend(
                [
                    {"label": "Ready", "value": str(ready)},
                    {"label": "Unsupported", "value": str(unsupported)},
                    {"label": "Deferred", "value": str(deferred)},
                    {"label": "Failed", "value": str(failed)},
                    {"label": "Images uploaded", "value": str(images)},
                    {"label": "Stale images removed", "value": str(stale_images)},
                ]
            )
            if per_mime_limit is not None:
                summary["highlights"].append(
                    {"label": "Per MIME cap", "value": str(per_mime_limit)}
                )
            summary["message"] = (
                f"Non-PDF extraction completed: {ready} ready, "
                f"{unsupported} unsupported, {deferred} deferred, {failed} failed."
            )
        else:
            summary["message"] = "Non-PDF extraction completed."
        return summary

    if panel_id == "library" and status == "completed":
        summary["message"] = "Library task completed."
        return summary

    return summary
