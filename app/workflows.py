"""Workflow and schedule runtime for Manzara."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db import ACTIVE_STATUSES, Database
from app.tasks import TaskRunner


class WorkflowService:
    """Executes chained workflows and triggers them by schedule."""

    def __init__(
        self,
        db: Database,
        runner: TaskRunner,
        *,
        shayan_snapshot_file: Path,
        tick_seconds: float = 1.0,
    ):
        self.db = db
        self.runner = runner
        self.shayan_snapshot_file = shayan_snapshot_file
        self.tick_seconds = tick_seconds

        self._stop_event = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start scheduler loop."""
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return

        self._stop_event.clear()
        self._initialize_enabled_schedules()

        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="workflow-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """Stop scheduler loop."""
        self._stop_event.set()
        thread = self._scheduler_thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def trigger_workflow(
        self,
        workflow_id: str,
        *,
        trigger_source: str,
        schedule_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start one workflow run if there is no overlapping active run."""
        workflow = self.db.get_workflow(workflow_id)
        if not workflow:
            return {
                "action": "noop",
                "reason": "workflow_not_found",
            }

        if trigger_source == "schedule" and not workflow.get("enabled", True):
            return {
                "action": "noop",
                "reason": "workflow_disabled",
            }

        active = self.db.get_active_workflow_run(workflow_id)
        if active:
            return {
                "action": "noop",
                "reason": "already_running",
                "workflow_run": active,
            }

        workflow_run_id = self.db.create_workflow_run(
            workflow_id=workflow_id,
            schedule_id=schedule_id,
            trigger_source=trigger_source,
            context={},
        )
        self.db.insert_event(
            "workflow.started",
            task_id=None,
            run_id=None,
            panel_id=workflow.get("panel_id"),
            payload={
                "workflow_id": workflow_id,
                "workflow_run_id": workflow_run_id,
                "trigger_source": trigger_source,
                "schedule_id": schedule_id,
            },
        )

        thread = threading.Thread(
            target=self._run_workflow,
            args=(workflow_run_id,),
            name=f"workflow-run-{workflow_run_id}",
            daemon=True,
        )
        thread.start()

        return {
            "action": "start",
            "workflow_run": self.db.get_workflow_run(workflow_run_id),
        }

    def configure_schedule(self, schedule_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Patch schedule and recalculate next run timestamp."""
        schedule = self.db.update_schedule(schedule_id, updates)
        if not schedule:
            return None

        if schedule.get("enabled"):
            next_run = self.compute_next_run_at(schedule)
            schedule = self.db.update_schedule(schedule_id, {"next_run_at": next_run})
        else:
            schedule = self.db.update_schedule(schedule_id, {"next_run_at": None})

        workflow = self.db.get_workflow(schedule["workflow_id"])
        self.db.insert_event(
            "schedule.updated",
            task_id=None,
            run_id=None,
            panel_id=workflow.get("panel_id") if workflow else None,
            payload={
                "schedule_id": schedule_id,
                "workflow_id": schedule["workflow_id"],
                "enabled": bool(schedule.get("enabled")),
                "day_of_week": int(schedule["day_of_week"]),
                "time_of_day": schedule["time_of_day"],
                "timezone": schedule["timezone"],
                "next_run_at": schedule.get("next_run_at"),
            },
        )
        return schedule

    def compute_next_run_at(
        self,
        schedule: Dict[str, Any],
        *,
        now_utc: Optional[datetime] = None,
    ) -> str:
        """Compute next UTC run timestamp for weekly schedule."""
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)

        timezone_name = str(schedule.get("timezone") or "UTC")
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("UTC")
            timezone_name = "UTC"

        if schedule.get("schedule_type") != "weekly":
            return now_utc.isoformat()

        day_of_week = int(schedule.get("day_of_week") or 1)
        day_of_week = max(1, min(7, day_of_week))
        hour, minute = self._parse_time_of_day(str(schedule.get("time_of_day") or "03:00"))

        local_now = now_utc.astimezone(zone)
        target_weekday = day_of_week - 1
        days_ahead = (target_weekday - local_now.weekday()) % 7

        candidate_date = local_now.date() + timedelta(days=days_ahead)
        candidate_local = datetime.combine(
            candidate_date,
            dt_time(hour=hour, minute=minute, tzinfo=zone),
        )

        if candidate_local <= local_now:
            candidate_local += timedelta(days=7)

        return candidate_local.astimezone(timezone.utc).isoformat()

    def _initialize_enabled_schedules(self) -> None:
        for schedule in self.db.list_enabled_schedules():
            if schedule.get("next_run_at"):
                continue
            next_run = self.compute_next_run_at(schedule)
            self.db.update_schedule(schedule["schedule_id"], {"next_run_at": next_run})

    def _scheduler_loop(self) -> None:
        while not self._stop_event.wait(self.tick_seconds):
            now_iso = datetime.now(timezone.utc).isoformat()
            due = self.db.list_due_schedules(now_iso)
            for schedule in due:
                self._trigger_due_schedule(schedule)

    def _trigger_due_schedule(self, schedule: Dict[str, Any]) -> None:
        workflow_id = schedule["workflow_id"]
        workflow = self.db.get_workflow(workflow_id)
        panel_id = workflow.get("panel_id") if workflow else None

        active = self.db.get_active_workflow_run(workflow_id)
        if active and schedule.get("overlap_policy") == "skip":
            next_run = self.compute_next_run_at(schedule)
            self.db.update_schedule(schedule["schedule_id"], {"next_run_at": next_run})
            self.db.insert_event(
                "schedule.skipped_overlap",
                task_id=None,
                run_id=None,
                panel_id=panel_id,
                payload={
                    "schedule_id": schedule["schedule_id"],
                    "workflow_id": workflow_id,
                    "active_workflow_run_id": active["workflow_run_id"],
                    "next_run_at": next_run,
                },
            )
            return

        result = self.trigger_workflow(
            workflow_id,
            trigger_source="schedule",
            schedule_id=schedule["schedule_id"],
        )
        next_run = self.compute_next_run_at(schedule)

        if result.get("action") == "start":
            self.db.update_schedule(
                schedule["schedule_id"],
                {
                    "last_run_at": datetime.now(timezone.utc).isoformat(),
                    "next_run_at": next_run,
                },
            )
            self.db.insert_event(
                "schedule.triggered",
                task_id=None,
                run_id=None,
                panel_id=panel_id,
                payload={
                    "schedule_id": schedule["schedule_id"],
                    "workflow_id": workflow_id,
                    "workflow_run_id": result["workflow_run"]["workflow_run_id"],
                    "next_run_at": next_run,
                },
            )
            return

        self.db.update_schedule(schedule["schedule_id"], {"next_run_at": next_run})
        self.db.insert_event(
            "schedule.skipped",
            task_id=None,
            run_id=None,
            panel_id=panel_id,
            payload={
                "schedule_id": schedule["schedule_id"],
                "workflow_id": workflow_id,
                "reason": result.get("reason", "unknown"),
                "next_run_at": next_run,
            },
        )

    def _run_workflow(self, workflow_run_id: int) -> None:
        workflow_run = self.db.get_workflow_run(workflow_run_id)
        if not workflow_run:
            return

        workflow = self.db.get_workflow(workflow_run["workflow_id"])
        if not workflow:
            self.db.update_workflow_run(
                workflow_run_id,
                status="failed",
                error_text="Workflow definition not found.",
                finished=True,
            )
            return

        panel_id = workflow.get("panel_id")
        context = dict(workflow_run.get("context") or {})
        self.db.update_workflow_run(workflow_run_id, status="running", context=context)

        try:
            for step in self.db.list_workflow_steps(workflow["workflow_id"]):
                condition = step.get("condition") or {}
                condition_ok = self._evaluate_step_condition(condition, context)
                if not condition_ok:
                    output = {
                        "reason": "condition_not_met",
                        "condition": condition,
                        "context": context,
                    }
                    self.db.create_workflow_step_run(
                        workflow_run_id=workflow_run_id,
                        step_order=int(step["step_order"]),
                        task_id=step.get("task_id"),
                        status="skipped",
                        output=output,
                    )
                    self.db.insert_event(
                        "workflow.step_skipped",
                        task_id=step.get("task_id"),
                        run_id=None,
                        panel_id=panel_id,
                        payload={
                            "workflow_id": workflow["workflow_id"],
                            "workflow_run_id": workflow_run_id,
                            "step_order": int(step["step_order"]),
                            "reason": "condition_not_met",
                        },
                    )
                    continue

                task_id = step.get("task_id")
                if step.get("step_type") != "task" or not task_id:
                    self.db.create_workflow_step_run(
                        workflow_run_id=workflow_run_id,
                        step_order=int(step["step_order"]),
                        task_id=task_id,
                        status="skipped",
                        output={"reason": "unsupported_step"},
                    )
                    continue

                pre_state = self._collect_pre_state(task_id)
                started = self.runner.start_task(task_id)
                if started.get("action") != "start":
                    reason = started.get("reason", "task_not_started")
                    self.db.create_workflow_step_run(
                        workflow_run_id=workflow_run_id,
                        step_order=int(step["step_order"]),
                        task_id=task_id,
                        status="failed",
                        output={"reason": reason},
                        error_text=f"Task did not start: {reason}",
                    )
                    self.db.update_workflow_run(
                        workflow_run_id,
                        status="failed",
                        context=context,
                        error_text=f"Step {step['step_order']} failed to start task {task_id}: {reason}",
                        finished=True,
                    )
                    self.db.insert_event(
                        "workflow.failed",
                        task_id=task_id,
                        run_id=None,
                        panel_id=panel_id,
                        payload={
                            "workflow_id": workflow["workflow_id"],
                            "workflow_run_id": workflow_run_id,
                            "error": reason,
                        },
                    )
                    return

                task_run_id = int(started["run"]["run_id"])
                step_run_id = self.db.create_workflow_step_run(
                    workflow_run_id=workflow_run_id,
                    step_order=int(step["step_order"]),
                    task_id=task_id,
                    status="running",
                    task_run_id=task_run_id,
                    output={},
                )

                self.db.insert_event(
                    "workflow.step_started",
                    task_id=task_id,
                    run_id=task_run_id,
                    panel_id=panel_id,
                    payload={
                        "workflow_id": workflow["workflow_id"],
                        "workflow_run_id": workflow_run_id,
                        "step_order": int(step["step_order"]),
                        "task_run_id": task_run_id,
                    },
                )

                task_run = self._wait_for_task_terminal(task_run_id)
                output, updates = self._collect_post_state(task_id, pre_state, task_run)
                context.update(updates)
                self.db.update_workflow_run(workflow_run_id, context=context)

                if task_run["status"] == "completed":
                    self.db.finish_workflow_step_run(
                        step_run_id,
                        status="completed",
                        output=output,
                    )
                    self.db.insert_event(
                        "workflow.step_completed",
                        task_id=task_id,
                        run_id=task_run_id,
                        panel_id=panel_id,
                        payload={
                            "workflow_id": workflow["workflow_id"],
                            "workflow_run_id": workflow_run_id,
                            "step_order": int(step["step_order"]),
                            "output": output,
                        },
                    )
                    continue

                if task_run["status"] == "stopped":
                    self.db.finish_workflow_step_run(
                        step_run_id,
                        status="stopped",
                        output=output,
                    )
                    self.db.update_workflow_run(
                        workflow_run_id,
                        status="stopped",
                        context=context,
                        finished=True,
                    )
                    self.db.insert_event(
                        "workflow.stopped",
                        task_id=task_id,
                        run_id=task_run_id,
                        panel_id=panel_id,
                        payload={
                            "workflow_id": workflow["workflow_id"],
                            "workflow_run_id": workflow_run_id,
                            "step_order": int(step["step_order"]),
                        },
                    )
                    return

                self.db.finish_workflow_step_run(
                    step_run_id,
                    status="failed",
                    output=output,
                    error_text=task_run.get("error_text") or "Task failed",
                )
                self.db.update_workflow_run(
                    workflow_run_id,
                    status="failed",
                    context=context,
                    error_text=(task_run.get("error_text") or f"Task {task_id} failed"),
                    finished=True,
                )
                self.db.insert_event(
                    "workflow.failed",
                    task_id=task_id,
                    run_id=task_run_id,
                    panel_id=panel_id,
                    payload={
                        "workflow_id": workflow["workflow_id"],
                        "workflow_run_id": workflow_run_id,
                        "step_order": int(step["step_order"]),
                        "error": task_run.get("error_text"),
                    },
                )
                return

            self.db.update_workflow_run(
                workflow_run_id,
                status="completed",
                context=context,
                finished=True,
            )
            self.db.insert_event(
                "workflow.completed",
                task_id=None,
                run_id=None,
                panel_id=panel_id,
                payload={
                    "workflow_id": workflow["workflow_id"],
                    "workflow_run_id": workflow_run_id,
                    "context": context,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive runtime path
            self.db.update_workflow_run(
                workflow_run_id,
                status="failed",
                context=context,
                error_text=str(exc),
                finished=True,
            )
            self.db.insert_event(
                "workflow.failed",
                task_id=None,
                run_id=None,
                panel_id=panel_id,
                payload={
                    "workflow_id": workflow["workflow_id"],
                    "workflow_run_id": workflow_run_id,
                    "error": str(exc),
                },
            )

    def _evaluate_step_condition(self, condition: Dict[str, Any], context: Dict[str, Any]) -> bool:
        if not condition:
            return True

        threshold = condition.get("requires_scan_new_items_gt")
        if threshold is not None:
            scan_count = int(context.get("scan_new_items_count", 0))
            return scan_count > int(threshold)

        return True

    def _wait_for_task_terminal(self, run_id: int, timeout_seconds: float = 60 * 60 * 24) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            run = self.db.get_run(run_id)
            if not run:
                return {
                    "run_id": run_id,
                    "status": "failed",
                    "error_text": "Task run not found",
                }
            if run["status"] not in ACTIVE_STATUSES:
                return run
            time.sleep(0.5)

        run = self.db.get_run(run_id)
        if run:
            return run
        return {
            "run_id": run_id,
            "status": "failed",
            "error_text": "Task timeout",
        }

    def _collect_pre_state(self, task_id: str) -> Dict[str, Any]:
        if task_id == "shayan.scan_changes":
            return {
                "scan_before_count": self._snapshot_entry_count(self.shayan_snapshot_file),
            }
        return {}

    def _collect_post_state(
        self,
        task_id: str,
        pre_state: Dict[str, Any],
        task_run: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        if task_id != "shayan.scan_changes":
            return {}, {}

        after_count = self._snapshot_entry_count(self.shayan_snapshot_file)
        before_count = int(pre_state.get("scan_before_count", 0))
        new_count = max(after_count - before_count, 0)

        output = {
            "scan_before_count": before_count,
            "scan_after_count": after_count,
            "scan_new_items_count": new_count,
            "task_status": task_run.get("status"),
        }

        updates = {
            "scan_before_count": before_count,
            "scan_after_count": after_count,
            "scan_new_items_count": new_count,
        }
        return output, updates

    def _snapshot_entry_count(self, path: Path) -> int:
        try:
            if not path.exists():
                return 0
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("entries"), dict):
                return len(payload["entries"])
            if isinstance(payload, dict):
                return len(payload)
            if isinstance(payload, list):
                return len(payload)
        except Exception:
            return 0
        return 0

    def _parse_time_of_day(self, value: str) -> tuple[int, int]:
        parts = value.split(":")
        if len(parts) != 2:
            return 3, 0
        try:
            hour = max(0, min(23, int(parts[0])))
            minute = max(0, min(59, int(parts[1])))
            return hour, minute
        except ValueError:
            return 3, 0
