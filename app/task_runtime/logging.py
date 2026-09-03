"""Task log streaming, redaction, and artifact helpers."""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import threading
from typing import Any, Dict, Optional, TextIO

from app.modules.maintenance.backup_s3_verify import capture_pgbackrest_s3_state
from app.run_log_store import (
    has_run_log_before,
    read_run_log,
    run_log_path,
    safe_task_slug,
)


class TaskLoggingMixin:
    """Logging and emitted-artifact behavior for ``TaskRunner``."""

    def _emit_task_log(
        self,
        *,
        run_id: int,
        task_id: str,
        panel_id: str,
        line: str,
    ) -> None:
        """Persist one runtime line in the run artifact log."""
        safe_line = self._sanitize_log_line(line)
        self._write_run_log(
            run_id=run_id,
            task_id=task_id,
            panel_id=panel_id,
            level="INFO",
            source="runtime",
            message=safe_line,
        )


    def _is_pgbackrest_backup_task(self, task: Dict[str, Any]) -> bool:
        task_id = str(task.get("task_id") or "")
        return task_id.startswith("maintenance.pgbackrest_backup_")


    def _pgbackrest_backup_kind(self, task: Dict[str, Any]) -> str:
        """Return the human-visible kind for a pgBackRest backup task."""
        task_id = str(task.get("task_id") or "")
        return "incremental" if task_id.endswith("_incr") else "full"


    def _capture_pgbackrest_s3_state(
        self,
        *,
        command_value: str,
    ) -> Dict[str, Any]:
        return capture_pgbackrest_s3_state(
            command_value=command_value,
            monocorpus_repo_path=Path(
                str(os.environ.get("MONOCORPUS_REPO_PATH") or "/home/tans1q/projects/monocorpus")
            ),
        )


    def _stream_stdout_lines(
        self,
        proc: subprocess.Popen[str],
        run_id: int,
        task_id: str,
        panel_id: str,
    ) -> None:
        """Stream stdout lines into the run artifact log without blocking finalization."""
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw_line in stream:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                safe_line = self._sanitize_log_line(line)
                self._write_run_log(
                    run_id=run_id,
                    task_id=task_id,
                    panel_id=panel_id,
                    level="INFO",
                    source="stdout",
                    message=safe_line,
                )
        except Exception as exc:
            if self._is_benign_log_stream_close(exc):
                return
            # Do not break task lifecycle on log-stream errors, but emit
            # actionable context for UI/artifact diagnostics.
            error_line = self._sanitize_log_line(f"log_stream_error={exc}")
            self._write_run_log(
                run_id=run_id,
                task_id=task_id,
                panel_id=panel_id,
                level="WARNING",
                source="runtime",
                message=error_line,
            )
            return


    def _heartbeat_until_stopped(
        self,
        run_id: int,
        stop_event: threading.Event,
        interval_seconds: float = 5.0,
    ) -> None:
        """Persist one bounded-cadence heartbeat independent of log volume."""
        while not stop_event.wait(max(1.0, float(interval_seconds))):
            try:
                self.db.heartbeat(run_id)
            except Exception:
                return


    @staticmethod
    def _is_benign_log_stream_close(exc: Exception) -> bool:
        text = str(exc or "").strip().lower()
        if not text:
            return False
        return "i/o operation on closed file" in text or "closed file" in text


    def _open_run_log(self, task: Dict[str, Any], run_id: int) -> tuple[Optional[TextIO], Optional[str]]:
        task_id = str(task.get("task_id") or "unknown")
        log_path = run_log_path(self._artifacts_root, task_id, run_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = log_path.open("a", encoding="utf-8")
            header = self._format_run_log(
                level="INFO",
                run_id=run_id,
                task_id=task_id,
                panel_id=str(task.get("panel_id") or "unknown"),
                source="runtime",
                message=f"log_path={log_path}",
            )
            handle.write(header + "\n")
            handle.flush()
            return handle, str(log_path)
        except Exception:
            return None, None


    def _artifact_output_path(self, task: Dict[str, Any], run_id: int) -> Path:
        task_id = str(task.get("task_id") or "unknown")
        task_dir = self._artifacts_root / self._safe_slug(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir / f"run-{run_id}.artifact.json"


    def _artifact_event_payload(self, artifacts: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(artifacts, dict) or not artifacts:
            return {}
        kind = str(artifacts.get("kind") or "").strip()
        if not kind:
            return {}
        payload: Dict[str, Any] = {"kind": kind}
        if kind == "library.book_preview_summary":
            for key in (
                "processed",
                "total",
                "ready",
                "partial",
                "failed",
                "uploaded_objects",
                "reused_objects",
                "downloaded_sources",
            ):
                payload[key] = int(artifacts.get(key) or 0)
            payload["stopped"] = bool(artifacts.get("stopped"))
            payload["recipe_version"] = str(artifacts.get("recipe_version") or "")
            return payload
        if kind == "library.non_pdf_extraction_summary":
            for key in (
                "processed",
                "total",
                "ready",
                "failed",
                "deferred",
                "unsupported",
                "downloaded_sources",
                "reused_sources",
                "uploaded_images",
                "reused_images",
                "uploaded_archives",
                "reused_archives",
                "checkpoint_raced",
                "deleted_stale_images",
            ):
                payload[key] = int(artifacts.get(key) or 0)
            payload["stopped"] = bool(artifacts.get("stopped"))
            payload["extractor_version"] = str(
                artifacts.get("extractor_version") or ""
            )
            payload["formats"] = (
                dict(artifacts.get("formats"))
                if isinstance(artifacts.get("formats"), dict)
                else {}
            )
            payload["per_mime_limit"] = artifacts.get("per_mime_limit")
            payload["max_automatic_attempts"] = int(
                artifacts.get("max_automatic_attempts") or 0
            )
            payload["retry_known_failures"] = bool(
                artifacts.get("retry_known_failures")
            )
            payload["mime_outcomes"] = (
                dict(artifacts.get("mime_outcomes"))
                if isinstance(artifacts.get("mime_outcomes"), dict)
                else {}
            )
            return payload
        if kind == "library.site_export_summary":
            for key in (
                "version",
                "documents_published",
                "documents_excluded",
                "entities",
                "collections",
                "classifications",
                "documents_with_previews",
            ):
                payload[key] = int(artifacts.get(key) or 0)
            for key in ("format", "revision", "bundle_path", "bundle_sha256"):
                payload[key] = str(artifacts.get(key) or "")
            payload["exclusion_reasons"] = (
                dict(artifacts.get("exclusion_reasons"))
                if isinstance(artifacts.get("exclusion_reasons"), dict)
                else {}
            )
            payload["stopped"] = bool(artifacts.get("stopped"))
            return payload
        return payload


    def _write_run_log(
        self,
        *,
        run_id: int,
        task_id: str,
        panel_id: str,
        level: str,
        source: str,
        message: str,
    ) -> None:
        with self._lock:
            handle = self._processes.get(run_id)
            log_file = (
                handle.log_file
                if handle and handle.log_file is not None
                else self._run_log_files.get(run_id)
            )
        safe_message = self._sanitize_log_line(message)
        line = self._format_run_log(
            level=level,
            run_id=run_id,
            task_id=task_id,
            panel_id=panel_id,
            source=source,
            message=safe_message,
        )
        self._write_run_log_to_handle(log_file, line)


    def _write_run_log_to_handle(self, log_file: Optional[TextIO], line: str) -> None:
        """Write one already-formatted line to a run artifact log handle."""
        if log_file is None:
            return
        try:
            with self._log_write_lock:
                log_file.write(line + "\n")
                log_file.flush()
        except Exception:
            return


    def _format_run_log(
        self,
        *,
        level: str,
        run_id: int,
        task_id: str,
        panel_id: str,
        source: str,
        message: str,
    ) -> str:
        ts = datetime.now(timezone.utc).isoformat()
        safe_message = str(message or "").replace("\n", "\\n")
        return (
            f"{ts} | {level.upper()} | "
            f"run_id={run_id} task_id={task_id} panel_id={panel_id} source={source} | "
            f"{safe_message}"
        )


    def _safe_slug(self, value: str) -> str:
        return safe_task_slug(value)


    def get_run_logs(
        self,
        *,
        task_id: str,
        run_id: int,
        after_log_id: int = 0,
        before_log_id: int | None = None,
        tail: bool = False,
        limit: int = 300,
    ) -> list[Dict[str, Any]]:
        """Read a bounded page from one file-backed run log."""
        return read_run_log(
            self._artifacts_root,
            task_id,
            run_id,
            after_log_id=after_log_id,
            before_log_id=before_log_id,
            tail=tail,
            limit=limit,
        )


    def has_run_logs_before(self, *, task_id: str, run_id: int, log_id: int) -> bool:
        """Return whether an older file-backed log page exists."""
        return has_run_log_before(self._artifacts_root, task_id, run_id, log_id)


    def _sanitize_log_line(self, line: str) -> str:
        """Mask common secret/token patterns before persisting user-visible logs."""
        text = str(line or "")
        if not text:
            return ""
        for pattern, replacement in self._LOG_REDACTION_PATTERNS:
            text = pattern.sub(replacement, text)
        return text
