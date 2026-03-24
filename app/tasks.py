"""Generic task execution runtime."""

from __future__ import annotations

import hashlib
import json
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

from app.db import Database
from app.runtime_states import (
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_RUNNING,
    TASK_RUN_STATUS_STARTING,
    resolve_task_terminal_status,
    task_terminal_event_type,
)
from app.modules.maintenance.backup_s3_verify import verify_backup_objects_in_s3


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
        self._artifacts_root = Path(__file__).resolve().parent.parent / "_artifacts" / "task_runs"
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

            backup_state_before: Optional[Dict[str, Any]] = None
            if self._is_pgbackrest_backup_task(task):
                backup_state_before = self._capture_pgbackrest_repo_state(
                    command_value=command["value"],
                    cwd=cwd,
                    sudo_password=sudo_password,
                )

            command_text, stdin_text = self._prepare_command(command["value"], sudo_password)

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
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            if log_thread is not None and log_thread.is_alive():
                log_thread.join(timeout=1.5)

            final_run = self.db.get_run(run_id)
            stop_mode = final_run.get("stop_mode") if final_run else None
            validation_error = None

            if stop_mode is None and exit_code == 0:
                validation_error = self._validate_success_conditions(
                    run_id=run_id,
                    task=task,
                    command_value=command["value"],
                    cwd=cwd,
                    sudo_password=sudo_password,
                    backup_state_before=backup_state_before,
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
        cwd: str,
        sudo_password: Optional[str],
        backup_state_before: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Return error message when task-specific success criteria are not met."""
        task_id = str(task.get("task_id") or "")
        if not task_id.startswith("maintenance.pgbackrest_backup_"):
            return None

        logs = self.db.get_logs(run_id, after_log_id=0, limit=5000)
        raw_lines = [str(row.get("line") or "") for row in logs]
        lines = [line.lower() for line in raw_lines]

        has_new_label = any("new backup label =" in line for line in lines)
        has_backup_size = any(" backup size =" in line for line in lines)
        if not (has_new_label and has_backup_size):
            missing: list[str] = []
            if not has_new_label:
                missing.append("new backup label")
            if not has_backup_size:
                missing.append("backup size")
            missing_text = ", ".join(missing)
            return (
                "Backup finished without repository-change evidence "
                f"({missing_text} marker not found in pgBackRest logs)."
            )

        if not backup_state_before or not backup_state_before.get("ok"):
            reason = str((backup_state_before or {}).get("error") or "unable to capture pre-backup state")
            return f"Backup repository state check failed before run: {reason}"

        backup_state_after = self._capture_pgbackrest_repo_state(
            command_value=command_value,
            cwd=cwd,
            sudo_password=sudo_password,
        )
        if not backup_state_after.get("ok"):
            reason = str(backup_state_after.get("error") or "unable to capture post-backup state")
            return f"Backup repository state check failed after run: {reason}"

        before_fp = str(backup_state_before.get("fingerprint") or "")
        after_fp = str(backup_state_after.get("fingerprint") or "")
        before_labels = set(backup_state_before.get("labels") or [])
        after_labels = set(backup_state_after.get("labels") or [])
        labels_added = sorted(after_labels - before_labels)

        # A changed repo fingerprint means pgBackRest repository metadata changed,
        # which implies object storage files were added/modified.
        if before_fp == after_fp:
            return (
                "Backup command exited successfully, but pgBackRest repository state did not change. "
                "Object storage appears unchanged."
            )

        self._emit_task_log(
            run_id=run_id,
            task_id=task["task_id"],
            panel_id=task["panel_id"],
            line=(
                "Backup repository state changed: "
                f"labels_before={len(before_labels)}, labels_after={len(after_labels)}, "
                f"labels_added={labels_added if labels_added else '[]'}"
            ),
        )

        s3_result = verify_backup_objects_in_s3(
            raw_lines,
            monocorpus_repo_path=Path(
                str(os.environ.get("MONOCORPUS_REPO_PATH") or "/home/tans1q/projects/monocorpus")
            ),
        )
        if not s3_result.get("ok"):
            reason = str(s3_result.get("error") or "backup files not found in S3")
            return f"S3 backup verification failed: {reason}"

        self._emit_task_log(
            run_id=run_id,
            task_id=task["task_id"],
            panel_id=task["panel_id"],
            line=(
                "S3 backup verification passed: "
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

    def _capture_pgbackrest_repo_state(
        self,
        *,
        command_value: str,
        cwd: str,
        sudo_password: Optional[str],
    ) -> Dict[str, Any]:
        info_command = self._derive_pgbackrest_info_command(command_value)
        if not info_command:
            return {
                "ok": False,
                "error": "could not derive pgBackRest info command from task command",
            }

        prepared_command, stdin_text = self._prepare_command(info_command, sudo_password)
        try:
            result = subprocess.run(
                prepared_command,
                cwd=cwd,
                shell=True,
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except FileNotFoundError:
            return {"ok": False, "error": "pgBackRest command not found"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "pgBackRest info command timed out"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        if int(result.returncode) != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            reason = stderr or stdout or f"exit code {result.returncode}"
            return {"ok": False, "error": reason[:400]}

        try:
            payload = json.loads((result.stdout or "").strip() or "[]")
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"invalid pgBackRest info JSON: {exc}"}

        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        labels = sorted(self._extract_pgbackrest_labels(payload))
        return {
            "ok": True,
            "fingerprint": fingerprint,
            "labels": labels,
        }

    def _derive_pgbackrest_info_command(self, command_value: str) -> Optional[str]:
        try:
            tokens = shlex.split(command_value, posix=True)
        except ValueError:
            return None
        if not tokens:
            return None

        if tokens[-1] == "backup":
            tokens = tokens[:-1]

        filtered: list[str] = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.startswith("--type="):
                i += 1
                continue
            if token == "--type":
                i += 2
                continue
            filtered.append(token)
            i += 1

        if "pgbackrest" not in filtered:
            return None
        filtered.extend(["info", "--output=json"])
        return shlex.join(filtered)

    def _extract_pgbackrest_labels(self, payload: Any) -> set[str]:
        labels: set[str] = set()
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            backups = item.get("backup")
            if not isinstance(backups, list):
                continue
            for backup in backups:
                if not isinstance(backup, dict):
                    continue
                label = backup.get("label")
                if isinstance(label, str) and label.strip():
                    labels.add(label.strip())
        return labels

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
