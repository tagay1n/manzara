"""Generic task execution runtime."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
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
from app.modules.maintenance.backup_s3_verify import wait_for_pgbackrest_s3_change
from app.run_artifacts import capture_pre_run_artifacts, collect_post_run_artifacts
from app.run_artifact_channel import RUN_ARTIFACT_PATH_ENV, read_run_artifact
from app.run_summary import build_structured_run_summary
from app.task_runtime.commands import TaskCommandMixin
from app.task_runtime.logging import TaskLoggingMixin
from app.task_runtime.process import ProcessHandle


class TaskRunner(TaskCommandMixin, TaskLoggingMixin):
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
        self._log_write_lock = threading.Lock()
        self._processes: Dict[int, ProcessHandle] = {}
        self._run_log_files: Dict[int, TextIO] = {}
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
        run = self.db.get_run(run_id) or {}
        if run.get("gemini_workers") is not None:
            task = dict(task)
            task["gemini_workers"] = int(run["gemini_workers"])
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


    def request_stop_run(self, run_id: int, *, mode: str = "graceful") -> None:
        """Request a stop for one known run without task-id ambiguity."""
        if mode not in {"graceful", "force"}:
            raise ValueError(f"Unsupported stop mode: {mode}")
        self._request_stop(int(run_id), mode=mode)


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
        heartbeat_stop = threading.Event()
        heartbeat_thread: Optional[threading.Thread] = None
        try:
            command = task["command"]
            cwd = task["cwd"]
            if command.get("mode") != "shell":
                raise ValueError("Unsupported command mode")

            run_log_file, run_log_path = self._open_run_log(task, run_id)
            if run_log_file is not None:
                with self._lock:
                    self._run_log_files[run_id] = run_log_file
            pre_artifacts = capture_pre_run_artifacts(task)
            artifact_output_path = self._artifact_output_path(task, run_id)

            backup_s3_state_before: Optional[Dict[str, Any]] = None
            if self._is_pgbackrest_backup_task(task):
                backup_kind = self._pgbackrest_backup_kind(task)
                self._emit_task_log(
                    run_id=run_id,
                    task_id=task["task_id"],
                    panel_id=task["panel_id"],
                    line=(
                        f"Preparing {backup_kind} pgBackRest backup; "
                        "capturing Backblaze repository state."
                    ),
                )
                backup_s3_state_before = self._capture_pgbackrest_s3_state(
                    command_value=command["value"],
                )
                if backup_s3_state_before.get("ok"):
                    self._emit_task_log(
                        run_id=run_id,
                        task_id=task["task_id"],
                        panel_id=task["panel_id"],
                        line=(
                            "Backblaze repository state captured: "
                            f"bucket={backup_s3_state_before.get('bucket')}, "
                            f"existing_backup_labels={backup_s3_state_before.get('label_count', 0)}."
                        ),
                    )
                else:
                    self._emit_task_log(
                        run_id=run_id,
                        task_id=task["task_id"],
                        panel_id=task["panel_id"],
                        line=(
                            "Backblaze repository preflight failed; the task will report "
                            "the detailed verification error after pgBackRest exits."
                        ),
                    )

            command_text, stdin_text = self._prepare_command(command["value"], sudo_password)
            proc_env = os.environ.copy()
            proc_env["MANZARA_TASK_RUN_ID"] = str(run_id)
            proc_env["MANZARA_TASK_ID"] = str(task.get("task_id") or "")
            proc_env["MANZARA_PANEL_ID"] = str(task.get("panel_id") or "")
            if task.get("gemini_workers") is not None:
                proc_env["MANZARA_GEMINI_WORKERS"] = str(task["gemini_workers"])
            proc_env[RUN_ARTIFACT_PATH_ENV] = str(artifact_output_path)

            if self._is_pgbackrest_backup_task(task):
                self._emit_task_log(
                    run_id=run_id,
                    task_id=task["task_id"],
                    panel_id=task["panel_id"],
                    line=(
                        f"Starting {self._pgbackrest_backup_kind(task)} pgBackRest backup; "
                        "live pgBackRest progress will follow."
                    ),
                )

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
            heartbeat_thread = threading.Thread(
                target=self._heartbeat_until_stopped,
                args=(run_id, heartbeat_stop),
                daemon=True,
                name=f"run-{run_id}-heartbeat",
            )
            heartbeat_thread.start()

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
            heartbeat_stop.set()
            if heartbeat_thread.is_alive():
                heartbeat_thread.join(timeout=1.0)
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
            log_rows = self.get_run_logs(
                task_id=str(task["task_id"]),
                run_id=run_id,
                after_log_id=0,
                limit=5000,
            )
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
            heartbeat_stop.set()
            if heartbeat_thread is not None and heartbeat_thread.is_alive():
                heartbeat_thread.join(timeout=1.0)
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
                self._run_log_files.pop(run_id, None)


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
