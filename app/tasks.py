"""Generic task execution runtime."""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

from app.artifacts import task_runs_dir
from app.db import Database
from app.runtime_states import (
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_RUNNING,
    TASK_RUN_STATUS_STARTING,
    resolve_task_terminal_status,
    task_terminal_event_type,
)
from app.modules.maintenance.backup_s3_verify import (
    capture_pgbackrest_s3_state,
    wait_for_pgbackrest_s3_change,
)
from app.run_artifacts import capture_pre_run_artifacts, collect_post_run_artifacts
from app.run_artifact_channel import RUN_ARTIFACT_PATH_ENV, read_run_artifact
from app.run_summary import build_structured_run_summary


@dataclass
class ProcessHandle:
    """In-memory link between a run id and active process."""

    run_id: int
    task_id: str
    panel_id: str
    proc: subprocess.Popen[str]
    log_file: Optional[TextIO] = None
    log_path: Optional[str] = None


class TaskRunner:
    """Runtime that starts/stops long-running task processes."""

    _LOG_REDACTION_PATTERNS = (
        (
            re.compile(
                r"(?i)(\bauthorization\b\s*:\s*(?:bearer|basic)\s+)([^\s,;]+)"
            ),
            r"\1<redacted>",
        ),
        (
            re.compile(
                r"(?i)(--repo1-s3-key-secret=)([^\s]+)"
            ),
            r"\1<redacted>",
        ),
        (
            re.compile(
                r"(?i)(--repo1-s3-key=)([^\s]+)"
            ),
            r"\1<redacted>",
        ),
        (
            re.compile(
                r"(?i)(\b(?:aws_secret_access_key|aws_access_key_id)\b\s*[=:]\s*)([^\s,;]+)"
            ),
            r"\1<redacted>",
        ),
        (
            re.compile(
                r"(?i)(\b(?:password|passwd|token|access_token|refresh_token|secret|api[_-]?key)\b\s*[=:]\s*)([^\s,;]+)"
            ),
            r"\1<redacted>",
        ),
        (
            re.compile(
                r'(?i)("?(?:password|passwd|token|access_token|refresh_token|secret|api[_-]?key|aws_secret_access_key|aws_access_key_id)"?\s*:\s*")([^"]+)(")'
            ),
            r"\1<redacted>\3",
        ),
        (
            re.compile(
                r'(?i)("authorization"\s*:\s*"(?:bearer|basic)\s+)([^"]+)(")'
            ),
            r"\1<redacted>\3",
        ),
        (
            re.compile(
                r"(?i)([?&](?:access_token|refresh_token|token|api[_-]?key|password|passwd|secret)=)([^&#\s]+)"
            ),
            r"\1<redacted>",
        ),
        (
            re.compile(
                r"(?i)(://[^:/\s]+:)([^@/\s]+)(@)"
            ),
            r"\1<redacted>\3",
        ),
    )

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._processes: Dict[int, ProcessHandle] = {}
        self._artifacts_root = task_runs_dir()
        self._artifacts_root.mkdir(parents=True, exist_ok=True)

    def check_task_start(
        self,
        task_id: str,
        *,
        sudo_password: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Preflight one task without creating a run row."""
        task = self.db.get_task(task_id)
        if not task:
            return {
                "ok": False,
                "reason": "task_not_found",
                "message": f"Unknown task id: {task_id}",
            }
        return self._check_sudo_requirements(task, sudo_password=sudo_password)

    def start_task(
        self,
        task_id: str,
        *,
        sudo_password: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start a task when no active run exists."""
        task = self.db.get_task(task_id)
        if not task:
            raise ValueError(f"Unknown task id: {task_id}")

        active = self.db.get_active_run_for_task(task_id)
        if active:
            return {
                "action": "noop",
                "reason": "already_running",
                "run": active,
            }

        preflight = self._check_sudo_requirements(task, sudo_password=sudo_password)
        if not preflight.get("ok", False):
            reason = str(preflight.get("reason") or "task_not_started")
            if reason in {"sudo_password_required", "sudo_password_invalid"}:
                return {
                    "action": reason,
                    "reason": reason,
                    "message": preflight.get("message"),
                    "task_id": task_id,
                }
            return {
                "action": "noop",
                "reason": reason,
                "message": preflight.get("message"),
            }

        run_id = self.db.create_run(task)
        self.db.insert_event(
            "task.started",
            task_id=task_id,
            run_id=run_id,
            panel_id=task["panel_id"],
            payload={"status": TASK_RUN_STATUS_STARTING},
        )

        thread = threading.Thread(
            target=self._run_task,
            args=(run_id, task, sudo_password),
            daemon=True,
            name=f"run-{run_id}",
        )
        thread.start()

        run = self.db.get_run(run_id)
        return {
            "action": "start",
            "run": run,
        }

    def toggle_task(
        self,
        task_id: str,
        *,
        sudo_password: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Toggle task state: start or stop/force-stop active run."""
        active = self.db.get_active_run_for_task(task_id)
        if not active:
            return self.start_task(task_id, sudo_password=sudo_password)

        run_id = int(active["run_id"])
        current_mode = active.get("stop_mode")
        if current_mode is None:
            self._request_stop(run_id, mode="graceful")
            return {
                "action": "stop_graceful",
                "run": self.db.get_run(run_id),
            }

        if current_mode == "graceful":
            self._request_stop(run_id, mode="force")
            return {
                "action": "stop_force",
                "run": self.db.get_run(run_id),
            }

        return {
            "action": "noop",
            "reason": "already_force_stopping",
            "run": self.db.get_run(run_id),
        }

    def stop_all_toggle(self) -> Dict[str, Any]:
        """Global two-step stop-all behavior for active tasks."""
        active_runs = self.db.list_active_runs()
        if not active_runs:
            return {
                "action": "noop",
                "reason": "no_running_tasks",
            }

        has_unarmed = any(run.get("stop_mode") is None for run in active_runs)
        mode = "graceful" if has_unarmed else "force"

        for run in active_runs:
            if mode == "graceful" and run.get("stop_mode") is not None:
                continue
            if mode == "force" and run.get("stop_mode") == "force":
                continue
            self._request_stop(int(run["run_id"]), mode=mode)

        self.db.insert_event(
            "system.stop_all_requested",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"mode": mode},
        )
        return {
            "action": f"stop_all_{mode}",
            "mode": mode,
            "affected_runs": [int(r["run_id"]) for r in active_runs],
        }

    def _request_stop(self, run_id: int, mode: str) -> None:
        """Persist stop mode and signal process if currently live."""
        run = self.db.get_run(run_id)
        if not run:
            return
        updated = self.db.set_stop_mode(run_id, mode)
        if not updated:
            return

        event_type = "task.stop_requested" if mode == "graceful" else "task.force_stop_requested"
        self.db.insert_event(
            event_type,
            task_id=run["task_id"],
            run_id=run_id,
            panel_id=run["panel_id"],
            payload={"mode": mode},
        )

        handle: Optional[ProcessHandle] = None
        with self._lock:
            handle = self._processes.get(run_id)
        target_pid: Optional[int] = None
        if handle and handle.proc.poll() is None:
            target_pid = int(handle.proc.pid)
        elif run.get("pid") is not None:
            target_pid = int(run["pid"])

        if target_pid is None:
            return

        sig = signal.SIGINT if mode == "graceful" else signal.SIGKILL
        try:
            pgid = os.getpgid(target_pid)
            os.killpg(pgid, sig)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            pass

        # Fallback to direct PID signal if process-group signaling is unavailable.
        try:
            os.kill(target_pid, sig)
        except (ProcessLookupError, PermissionError):
            return

    def _run_task(
        self,
        run_id: int,
        task: Dict[str, Any],
        sudo_password: Optional[str] = None,
    ) -> None:
        """Run task process and persist logs/events until completion."""
        run_log_file: Optional[TextIO] = None
        run_log_path: Optional[str] = None
        try:
            command = task["command"]
            cwd = task["cwd"]
            if command.get("mode") != "shell":
                raise ValueError("Unsupported command mode")

            run_log_file, run_log_path = self._open_run_log(task, run_id)
            pre_artifacts = capture_pre_run_artifacts(task)
            artifact_output_path = self._artifact_output_path(task, run_id)

            backup_s3_state_before: Optional[Dict[str, Any]] = None
            if self._is_pgbackrest_backup_task(task):
                backup_s3_state_before = self._capture_pgbackrest_s3_state(
                    command_value=command["value"],
                )

            command_text, stdin_text = self._prepare_command(command["value"], sudo_password)
            proc_env = os.environ.copy()
            proc_env["MANZARA_TASK_RUN_ID"] = str(run_id)
            proc_env["MANZARA_TASK_ID"] = str(task.get("task_id") or "")
            proc_env["MANZARA_PANEL_ID"] = str(task.get("panel_id") or "")
            proc_env[RUN_ARTIFACT_PATH_ENV] = str(artifact_output_path)

            proc = subprocess.Popen(
                command_text,
                cwd=cwd,
                shell=True,
                stdin=subprocess.PIPE if stdin_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                preexec_fn=os.setsid,
                env=proc_env,
            )

            handle = ProcessHandle(
                run_id=run_id,
                task_id=task["task_id"],
                panel_id=task["panel_id"],
                proc=proc,
                log_file=run_log_file,
                log_path=run_log_path,
            )
            with self._lock:
                self._processes[run_id] = handle

            self._write_run_log(
                run_id=run_id,
                task_id=task["task_id"],
                panel_id=task["panel_id"],
                level="INFO",
                source="runtime",
                message=f"spawned pid={proc.pid} cwd={cwd}",
            )

            self.db.mark_run_started(run_id, proc.pid)
            self.db.insert_event(
                "task.progress",
                task_id=task["task_id"],
                run_id=run_id,
                panel_id=task["panel_id"],
                payload={"status": TASK_RUN_STATUS_RUNNING},
            )

            if stdin_text is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(stdin_text)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass

            log_thread: Optional[threading.Thread] = None
            if proc.stdout is not None:
                log_thread = threading.Thread(
                    target=self._stream_stdout_lines,
                    args=(proc, run_id, task["task_id"], task["panel_id"]),
                    daemon=True,
                    name=f"run-{run_id}-log-stream",
                )
                log_thread.start()

            exit_code = proc.wait()
            if log_thread is not None and log_thread.is_alive():
                log_thread.join(timeout=1.5)
            # An inherited stdout descriptor can outlive the task process. Closing
            # the stream while the reader is blocked would wait for that unrelated
            # descendant and delay the task's terminal state.
            if proc.stdout is not None and (log_thread is None or not log_thread.is_alive()):
                try:
                    proc.stdout.close()
                except OSError:
                    pass

            final_run = self.db.get_run(run_id)
            stop_mode = final_run.get("stop_mode") if final_run else None
            validation_error = None

            if stop_mode is None and exit_code == 0:
                validation_error = self._validate_success_conditions(
                    run_id=run_id,
                    task=task,
                    command_value=command["value"],
                    backup_s3_state_before=backup_s3_state_before,
                )
                if validation_error:
                    self._emit_task_log(
                        run_id=run_id,
                        task_id=task["task_id"],
                        panel_id=task["panel_id"],
                        line=validation_error,
                    )
                    exit_code = 90

            status = resolve_task_terminal_status(exit_code=exit_code, stop_mode=stop_mode)
            event_type = task_terminal_event_type(status)

            error_text = None
            if status == TASK_RUN_STATUS_FAILED:
                error_text = validation_error or f"Process exited with code {exit_code}"

            self._write_run_log(
                run_id=run_id,
                task_id=task["task_id"],
                panel_id=task["panel_id"],
                level="INFO" if status != TASK_RUN_STATUS_FAILED else "ERROR",
                source="runtime",
                message=(
                    f"final status={status} exit_code={exit_code}"
                    + (f" error={error_text}" if error_text else "")
                ),
            )

            self.db.finish_run(
                run_id=run_id,
                status=status,
                exit_code=exit_code,
                error_text=error_text,
            )
            run_row = self.db.get_run(run_id) or {}
            log_rows = self.db.get_logs(run_id, after_log_id=0, limit=5000)
            emitted_artifact = read_run_artifact(artifact_output_path)
            task_artifacts = collect_post_run_artifacts(
                task,
                status=str(status),
                pre_state=pre_artifacts,
                artifact_payload=emitted_artifact,
            )
            artifact_event_payload = self._artifact_event_payload(task_artifacts)
            if artifact_event_payload:
                self.db.insert_event(
                    "task.artifact",
                    task_id=task["task_id"],
                    run_id=run_id,
                    panel_id=task["panel_id"],
                    payload=artifact_event_payload,
                )
            summary = build_structured_run_summary(
                task_id=str(task["task_id"]),
                panel_id=str(task["panel_id"]),
                status=str(status),
                exit_code=exit_code,
                error_text=error_text,
                stop_mode=run_row.get("stop_mode"),
                started_at=run_row.get("started_at"),
                finished_at=run_row.get("finished_at"),
                log_lines=[str(item.get("line") or "") for item in log_rows],
                artifacts=task_artifacts,
            )
            self.db.update_run_summary(run_id, summary)
            self.db.insert_event(
                event_type,
                task_id=task["task_id"],
                run_id=run_id,
                panel_id=task["panel_id"],
                payload={"exit_code": exit_code, "status": status},
            )
        except Exception as exc:  # pragma: no cover - defensive runtime path
            self._write_run_log_to_handle(
                run_log_file,
                self._format_run_log(
                    level="ERROR",
                    run_id=run_id,
                    task_id=task.get("task_id", "unknown"),
                    panel_id=task.get("panel_id", "unknown"),
                    source="runtime",
                    message=self._sanitize_log_line(f"exception={exc}"),
                ),
            )
            self.db.finish_run(
                run_id=run_id,
                status=TASK_RUN_STATUS_FAILED,
                exit_code=None,
                error_text=str(exc),
            )
            failed_row = self.db.get_run(run_id) or {}
            failed_summary = build_structured_run_summary(
                task_id=str(task.get("task_id") or "unknown"),
                panel_id=str(task.get("panel_id") or "unknown"),
                status=TASK_RUN_STATUS_FAILED,
                exit_code=None,
                error_text=str(exc),
                stop_mode=failed_row.get("stop_mode"),
                started_at=failed_row.get("started_at"),
                finished_at=failed_row.get("finished_at"),
                log_lines=[],
                artifacts={},
            )
            self.db.update_run_summary(run_id, failed_summary)
            self.db.insert_event(
                task_terminal_event_type(TASK_RUN_STATUS_FAILED),
                task_id=task["task_id"],
                run_id=run_id,
                panel_id=task["panel_id"],
                payload={"error": str(exc)},
            )
        finally:
            with self._lock:
                handle = self._processes.get(run_id)
            log_file_to_close = handle.log_file if handle and handle.log_file is not None else run_log_file
            if log_file_to_close is not None:
                try:
                    log_file_to_close.flush()
                    log_file_to_close.close()
                except Exception:
                    pass
            with self._lock:
                self._processes.pop(run_id, None)

    def _validate_success_conditions(
        self,
        *,
        run_id: int,
        task: Dict[str, Any],
        command_value: str,
        backup_s3_state_before: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Return error message when task-specific success criteria are not met."""
        task_id = str(task.get("task_id") or "")
        if not task_id.startswith("maintenance.pgbackrest_backup_"):
            return None

        if not backup_s3_state_before or not backup_s3_state_before.get("ok"):
            reason = str((backup_s3_state_before or {}).get("error") or "unable to capture pre-backup S3 state")
            return f"S3 backup verification failed before run: {reason}"

        s3_result = wait_for_pgbackrest_s3_change(
            before_state=backup_s3_state_before,
            command_value=command_value,
            monocorpus_repo_path=Path(
                str(os.environ.get("MONOCORPUS_REPO_PATH") or "/home/tans1q/projects/monocorpus")
            ),
        )
        if not s3_result.get("ok"):
            reason = str(s3_result.get("error") or "backup files not found in S3")
            return f"S3 backup verification failed: {reason}"

        labels_before = backup_s3_state_before.get("labels") or []
        labels_added = s3_result.get("labels_added") or []
        labels_updated = s3_result.get("labels_updated") or []

        self._emit_task_log(
            run_id=run_id,
            task_id=task["task_id"],
            panel_id=task["panel_id"],
            line=(
                "S3 backup verification passed: "
                f"labels_before={len(labels_before)}, "
                f"labels_added={labels_added if labels_added else '[]'}, "
                f"labels_updated={labels_updated if labels_updated else '[]'}, "
                f"mode={s3_result.get('verification_mode') or 'new_label'}, "
                f"bucket={s3_result.get('bucket')}, "
                f"prefix={s3_result.get('prefix')}, "
                f"objects={s3_result.get('object_count')}"
            ),
        )
        return None

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

    def _check_sudo_requirements(
        self,
        task: Dict[str, Any],
        *,
        sudo_password: Optional[str],
    ) -> Dict[str, Any]:
        """Detect whether command requires sudo password before task start."""
        command = str((task.get("command") or {}).get("value") or "")
        parsed = self._split_leading_sudo_command(command)
        if not parsed:
            return {"ok": True}

        options, command_tokens = parsed
        probe_tokens, probe_input = self._build_sudo_probe(
            options=options,
            command_tokens=command_tokens,
            sudo_password=sudo_password,
        )
        try:
            result = subprocess.run(
                probe_tokens,
                input=probe_input,
                capture_output=True,
                text=True,
                timeout=12,
            )
        except FileNotFoundError:
            return {
                "ok": False,
                "reason": "sudo_missing",
                "message": "sudo is not installed on this machine.",
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "reason": "sudo_probe_timeout",
                "message": "Timed out while checking sudo access.",
            }

        if int(result.returncode) == 0:
            return {"ok": True}

        output = f"{result.stdout}\n{result.stderr}".strip().lower()
        if self._sudo_password_invalid(output):
            return {
                "ok": False,
                "reason": "sudo_password_invalid",
                "message": "Sudo password is incorrect.",
            }

        if self._sudo_password_required(output):
            if sudo_password:
                return {
                    "ok": False,
                    "reason": "sudo_password_invalid",
                    "message": "Sudo password is incorrect.",
                }
            return {
                "ok": False,
                "reason": "sudo_password_required",
                "message": "Sudo password is required for this command.",
            }

        if self._sudo_access_denied(output):
            return {
                "ok": False,
                "reason": "sudo_access_denied",
                "message": "Current user is not allowed to run this sudo command.",
            }

        return {
            "ok": False,
            "reason": "sudo_unavailable",
            "message": "Unable to validate sudo access for this command.",
        }

    def _prepare_command(
        self,
        command: str,
        sudo_password: Optional[str],
    ) -> tuple[str, Optional[str]]:
        if not sudo_password:
            return command, None

        parsed = self._split_leading_sudo_command(command)
        if not parsed:
            return command, None

        options, command_tokens = parsed
        rebuilt_options: list[str] = []
        i = 0
        saw_stdin_flag = False
        saw_prompt = False
        while i < len(options):
            token = options[i]
            if token == "-n":
                i += 1
                continue
            if token == "-S":
                saw_stdin_flag = True
                rebuilt_options.append(token)
                i += 1
                continue
            if token == "-p":
                saw_prompt = True
                i += 2
                continue
            if token.startswith("--prompt="):
                saw_prompt = True
                i += 1
                continue
            rebuilt_options.append(token)
            i += 1

        if not saw_stdin_flag:
            rebuilt_options.insert(0, "-S")
        if not saw_prompt:
            rebuilt_options[0:0] = ["-p", ""]

        rebuilt = ["sudo", *rebuilt_options, *command_tokens]
        return shlex.join(rebuilt), f"{sudo_password}\n"

    def _build_sudo_probe(
        self,
        *,
        options: list[str],
        command_tokens: list[str],
        sudo_password: Optional[str],
    ) -> tuple[list[str], Optional[str]]:
        sanitized: list[str] = []
        i = 0
        while i < len(options):
            token = options[i]
            if token in {"-n", "-S"}:
                i += 1
                continue
            if token == "-p":
                i += 2
                continue
            if token.startswith("--prompt="):
                i += 1
                continue
            sanitized.append(token)
            i += 1

        # Validate sudo policy for this exact command without executing it.
        probe_suffix = ["-l", "--", *command_tokens]
        if sudo_password:
            return ["sudo", "-S", "-p", "", *sanitized, *probe_suffix], f"{sudo_password}\n"
        return ["sudo", "-n", *sanitized, *probe_suffix], None

    def _split_leading_sudo_command(self, command: str) -> Optional[tuple[list[str], list[str]]]:
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return None
        if not tokens or tokens[0] != "sudo":
            return None

        options: list[str] = []
        i = 1
        while i < len(tokens):
            token = tokens[i]
            if token == "--":
                i += 1
                break
            if not token.startswith("-"):
                break
            options.append(token)
            if token in {"-u", "-g", "-h", "-p", "-r", "-t", "-C", "-T"} and i + 1 < len(tokens):
                options.append(tokens[i + 1])
                i += 2
                continue
            i += 1

        command_tokens = tokens[i:]
        if not command_tokens:
            return None
        return options, command_tokens

    def _sudo_password_required(self, text: str) -> bool:
        return any(
            marker in text
            for marker in (
                "a password is required",
                "password is required",
                "terminal is required",
                "no tty present and no askpass program specified",
            )
        )

    def _sudo_password_invalid(self, text: str) -> bool:
        return any(
            marker in text
            for marker in (
                "incorrect password",
                "sorry, try again",
            )
        )

    def _sudo_access_denied(self, text: str) -> bool:
        return any(
            marker in text
            for marker in (
                "not in the sudoers",
                "is not allowed to execute",
                "is not allowed to run sudo",
            )
        )
