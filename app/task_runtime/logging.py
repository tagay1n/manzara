"""Task log streaming, redaction, and artifact helpers."""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, Optional, TextIO

from app.modules.maintenance.backup_s3_verify import capture_pgbackrest_s3_state


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
        """Persist one runtime log line and broadcast over SSE."""
        safe_line = self._sanitize_log_line(line)
        self.db.append_log(run_id, stream="stdout", line=safe_line)
        self.db.insert_event(
            "task.log",
            task_id=task_id,
            run_id=run_id,
            panel_id=panel_id,
            payload={"line": safe_line},
        )
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
        """Stream stdout lines into run logs/events without blocking task finalization."""
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw_line in stream:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                safe_line = self._sanitize_log_line(line)
                self.db.append_log(run_id, stream="stdout", line=safe_line)
                self.db.heartbeat(run_id)
                self.db.insert_event(
                    "task.log",
                    task_id=task_id,
                    run_id=run_id,
                    panel_id=panel_id,
                    payload={"line": safe_line},
                )
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
            # actionable context for UI/DB/artifact diagnostics.
            error_line = self._sanitize_log_line(f"log_stream_error={exc}")
            try:
                self.db.append_log(run_id, stream="stderr", line=error_line)
                self.db.insert_event(
                    "task.log",
                    task_id=task_id,
                    run_id=run_id,
                    panel_id=panel_id,
                    payload={"line": error_line},
                )
            except Exception:
                pass
            self._write_run_log(
                run_id=run_id,
                task_id=task_id,
                panel_id=panel_id,
                level="WARNING",
                source="runtime",
                message=error_line,
            )
            return


    @staticmethod
    def _is_benign_log_stream_close(exc: Exception) -> bool:
        text = str(exc or "").strip().lower()
        if not text:
            return False
        return "i/o operation on closed file" in text or "closed file" in text


    def _open_run_log(self, task: Dict[str, Any], run_id: int) -> tuple[Optional[TextIO], Optional[str]]:
        task_id = str(task.get("task_id") or "unknown")
        task_dir = self._artifacts_root / self._safe_slug(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        log_path = task_dir / f"run-{run_id}.log"
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
        if kind == "shayan.snapshot_diff":
            payload["episodes_added"] = int(artifacts.get("episodes_added") or 0)
            payload["episodes_changed"] = int(artifacts.get("episodes_changed") or 0)
            payload["episodes_removed"] = int(artifacts.get("episodes_removed") or 0)
            return payload
        if kind == "shayan.download_summary":
            payload["downloaded"] = int(artifacts.get("downloaded") or 0)
            payload["failed"] = int(artifacts.get("failed") or 0)
            payload["manifest_added"] = int(artifacts.get("manifest_added") or 0)
            payload["manifest_changed"] = int(artifacts.get("manifest_changed") or 0)
            return payload
        if kind == "shayan.upload_yadisk_summary":
            payload["uploaded"] = int(artifacts.get("uploaded") or 0)
            payload["failed"] = int(artifacts.get("failed") or 0)
            payload["missing_local"] = int(artifacts.get("missing_local") or 0)
            payload["deleted_local"] = int(artifacts.get("deleted_local") or 0)
            payload["hash_mismatch"] = int(artifacts.get("hash_mismatch") or 0)
            return payload
        if kind == "shayan.yadisk_webdav_transfer_summary":
            payload["copied"] = int(artifacts.get("copied") or 0)
            payload["reused"] = int(artifacts.get("reused") or 0)
            payload["failed"] = int(artifacts.get("failed") or 0)
            payload["bytes_copied"] = int(artifacts.get("bytes_copied") or 0)
            payload["stopped"] = bool(artifacts.get("stopped"))
            return payload
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
            log_file = handle.log_file if handle else None
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
        text = str(value or "").strip().lower()
        if not text:
            return "unknown"
        return re.sub(r"[^a-z0-9._-]+", "_", text)


    def _sanitize_log_line(self, line: str) -> str:
        """Mask common secret/token patterns before persisting user-visible logs."""
        text = str(line or "")
        if not text:
            return ""
        for pattern, replacement in self._LOG_REDACTION_PATTERNS:
            text = pattern.sub(replacement, text)
        return text
