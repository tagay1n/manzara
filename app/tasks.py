"""Generic task execution runtime."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.db import Database


@dataclass
class ProcessHandle:
    """In-memory link between a run id and active process."""

    run_id: int
    task_id: str
    panel_id: str
    proc: subprocess.Popen[str]


class TaskRunner:
    """Runtime that starts/stops long-running task processes."""

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._processes: Dict[int, ProcessHandle] = {}

    def start_task(self, task_id: str) -> Dict[str, Any]:
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

        run_id = self.db.create_run(task)
        self.db.insert_event(
            "task.started",
            task_id=task_id,
            run_id=run_id,
            panel_id=task["panel_id"],
            payload={"status": "starting"},
        )

        thread = threading.Thread(
            target=self._run_task,
            args=(run_id, task),
            daemon=True,
            name=f"run-{run_id}",
        )
        thread.start()

        run = self.db.get_run(run_id)
        return {
            "action": "start",
            "run": run,
        }

    def toggle_task(self, task_id: str) -> Dict[str, Any]:
        """Toggle task state: start or stop/force-stop active run."""
        active = self.db.get_active_run_for_task(task_id)
        if not active:
            return self.start_task(task_id)

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

    def _run_task(self, run_id: int, task: Dict[str, Any]) -> None:
        """Run task process and persist logs/events until completion."""
        try:
            command = task["command"]
            cwd = task["cwd"]
            if command.get("mode") != "shell":
                raise ValueError("Unsupported command mode")

            proc = subprocess.Popen(
                command["value"],
                cwd=cwd,
                shell=True,
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
            )
            with self._lock:
                self._processes[run_id] = handle

            self.db.mark_run_started(run_id, proc.pid)
            self.db.insert_event(
                "task.progress",
                task_id=task["task_id"],
                run_id=run_id,
                panel_id=task["panel_id"],
                payload={"status": "running"},
            )

            if proc.stdout:
                for raw_line in proc.stdout:
                    line = raw_line.rstrip("\n")
                    if not line:
                        continue
                    self.db.append_log(run_id, stream="stdout", line=line)
                    self.db.heartbeat(run_id)
                    self.db.insert_event(
                        "task.log",
                        task_id=task["task_id"],
                        run_id=run_id,
                        panel_id=task["panel_id"],
                        payload={"line": line},
                    )

            exit_code = proc.wait()
            final_run = self.db.get_run(run_id)
            stop_mode = final_run.get("stop_mode") if final_run else None

            if stop_mode is not None:
                status = "stopped"
                event_type = "task.stopped"
            elif exit_code == 0:
                status = "completed"
                event_type = "task.completed"
            else:
                status = "failed"
                event_type = "task.failed"

            error_text = None
            if status == "failed":
                error_text = f"Process exited with code {exit_code}"

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
            self.db.finish_run(
                run_id=run_id,
                status="failed",
                exit_code=None,
                error_text=str(exc),
            )
            self.db.insert_event(
                "task.failed",
                task_id=task["task_id"],
                run_id=run_id,
                panel_id=task["panel_id"],
                payload={"error": str(exc)},
            )
        finally:
            with self._lock:
                self._processes.pop(run_id, None)
