"""Compact presentation of existing run states, counters, and summaries."""

from __future__ import annotations

from datetime import datetime, timezone
import math

from app.runtime_states import TASK_RUN_STATUS_COMPLETED
from app.task_runtime.logging import redact


def elapsed(run: dict) -> str:
    try:
        start = datetime.fromisoformat(run["started_at"])
        end = datetime.fromisoformat(run["finished_at"]) if run.get("finished_at") else datetime.now(timezone.utc)
        return duration(max(0, int((end - start).total_seconds())))
    except (KeyError, TypeError, ValueError):
        return "--:--"


def duration(seconds: int) -> str:
    return (f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
            if seconds >= 3600 else f"{seconds // 60:02d}:{seconds % 60:02d}")


def status(run: dict) -> str:
    summary = run.get("summary") or {}
    return ("deferred" if run.get("status") == TASK_RUN_STATUS_COMPLETED and summary.get("outcome") == "deferred"
            else str(run.get("status", "unknown")))


def progress_text(run: dict, width: int) -> str:
    progress = run.get("progress") or {}
    if progress.get("phase") == "discovering":
        return "discovering candidates"
    total = progress.get("total")
    current = progress.get("current", 0) if run.get("task_id") == "library.extract_non_pdf" else progress.get("resolved", progress.get("current", 0))
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
        return str(progress.get("phase") or "working")
    if not isinstance(current, int) or isinstance(current, bool):
        return str(progress.get("phase") or "working")
    count = f"{current}/{total}"
    if width < 70:
        return count
    filled = min(12, max(0, int(12 * current / total)))
    return f"{'━' * filled}{'─' * (12 - filled)}  {count}"


def provider_wait(payload: dict) -> str | None:
    try:
        until = datetime.fromisoformat(payload["wait_until"])
        seconds = math.ceil((until - datetime.now(timezone.utc)).total_seconds())
        if seconds <= 0:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    worker = payload.get("worker_id")
    subject = f"worker {worker}" if worker else "request pacing"
    mode = payload.get("mode")
    return f"Provider {subject}: {mode + ' · ' if isinstance(mode, str) else ''}{seconds}s wait"


def summary_text(run: dict | None, title: str, *, completion: bool = False,
                 failure: str | None = None, presentation_status: str | None = None) -> str:
    if run is None:
        return f"{title} · no saved run summary yet."
    summary = run.get("summary") or {}
    options = summary.get("options") or {}
    outcome = "failed" if failure else presentation_status or status(run)
    prefix = "" if completion else "Summary · "
    lines = [f"{prefix}{outcome.capitalize()} · {title} · run {run['run_id']} · {elapsed(run)}"]
    if summary.get("message"):
        lines.append(str(summary["message"]))
    keys = (("total", "eligible_total", "processed", "succeeded", "not_person", "unusable", "skipped", "failed", "deferred", "remaining")
            if summary.get("kind") == "library.personality_normalization_summary"
            else ("cluster_count", "singleton_count", "unresolved_count", "conflicting_groups", "new_proposals")
            if summary.get("kind") == "library.publisher_merge_summary"
            else ("scanned", "eligible", "excluded", "new_collection_proposals", "attachment_proposals",
                  "proposals_created", "proposals_updated", "proposals_reused", "proposals_superseded",
                  "reviewed_proposals_preserved")
            if summary.get("kind") == "library.collection_discovery_summary"
            else ("total", "processed", "succeeded", "already_complete", "review_required", "failed", "terminal",
                  "source_deferred", "quota_deferred", "service_deferred", "checkpoint_raced", "remaining")
            if summary.get("kind") in {"library.metadata_extraction_summary", "library.metadata_evaluation_summary"}
            else ("total", "processed", "ready", "failed", "skipped", "selected_pages", "uploaded_objects")
            if summary.get("kind") == "library.catalog_preview_summary"
            else ("total", "processed", "ready", "failed", "deferred", "unsupported", "corrupted", "checkpoint_raced"))
    counts = [f"{key.replace('_', ' ')}: {summary[key]}" for key in keys if key in summary]
    if not counts:
        counts = [f"{key}: {value}" for key, value in (run.get("progress") or {}).items()
                  if isinstance(value, int) and not isinstance(value, bool) and key != "percent"]
    if counts:
        lines.append(" · ".join(counts))
    if summary.get("kind") == "library.collection_discovery_summary" and outcome == "completed":
        lines.append("Collection proposals saved for later explicit review.")
    if summary.get("kind") == "library.publisher_merge_summary":
        if summary.get("analysis_id") is not None:
            lines.append(f"Analysis: {summary['analysis_id']} · scope: {summary.get('scope', 'unknown')}")
        if summary.get("no_analysis_needed"):
            lines.append("No publisher analysis needed for the current inventory and scope.")
        elif outcome == "completed":
            lines.append("Proposals saved for later explicit review; publisher identities were not changed.")
    if outcome == "deferred":
        lines.append("Work remains deferred; inspect provider/retry restrictions before resuming with /run.")
    error = failure or run.get("error_text") or summary.get("error")
    if error:
        lines.append("Error: " + str(error))
    if not completion:
        lines.append(f"Workers: {options.get('workers', run.get('gemini_workers') or 1)} · limit: {options.get('limit') or 'unlimited'}")
        if options.get("per_mime_limit") is not None or options.get("retry_known_failures") or options.get("only_md5s"):
            lines.append(restrictions(options))
        attempts = summary.get("model_attempts") or {}
        if attempts:
            lines.append("Model attempts: " + ", ".join(f"{model}: {count}" for model, count in attempts.items()))
    for label, key in (("Workspace", "workspace_path"), ("Items", "items_path"), ("Proposals", "proposals_path"),
                       ("Artifact", "artifact_path"), ("Log", "log_path")):
        if summary.get(key):
            lines.append(f"{label}: {summary[key]}")
    return redact("\n".join(lines))


def restrictions(options: dict) -> str:
    lines = [f"Explicit retries: {options.get('retry_known_failures', False)}"]
    if options.get("per_mime_limit") is not None:
        lines.append(f"MIME cohort cap: {options['per_mime_limit']}")
    if options.get("only_md5s"):
        lines.append("Source cohort: " + ", ".join(options["only_md5s"]))
    return "\n".join(lines)
