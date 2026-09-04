"""Runtime service for the singleton editable task conveyor."""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Mapping, Optional

from app.db import Database
from app.repositories.conveyor import ConveyorRevisionConflict
from app.runtime_states import (
    CONVEYOR_RUN_STATUS_COMPLETED,
    CONVEYOR_RUN_STATUS_FAILED,
    CONVEYOR_RUN_STATUS_STOPPED,
    TASK_RUN_ACTIVE_STATUSES,
    TASK_RUN_STATUS_COMPLETED,
    TASK_RUN_STATUS_FAILED,
    TASK_RUN_STATUS_STOPPED,
)
from app.tasks import TaskRunner


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class ConveyorValidationError(ValueError):
    """Raised when a conveyor definition violates its public contract."""


class ConveyorEditConflict(RuntimeError):
    """Raised when an edit would mutate already-claimed work."""


class ConveyorService:
    """Validate, edit, and execute one global staged task conveyor."""

    MAX_STAGES = 50
    MAX_ITEMS = 100

    def __init__(
        self,
        db: Database,
        runner: TaskRunner,
        *,
        poll_seconds: float = 0.25,
    ) -> None:
        self.db = db
        self.runner = runner
        self.poll_seconds = poll_seconds
        self._trigger_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def normalize_stages(self, raw_stages: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw_stages, list):
            raise ConveyorValidationError("stages must be a list")
        if len(raw_stages) > self.MAX_STAGES:
            raise ConveyorValidationError(
                f"stages must contain at most {self.MAX_STAGES} rows"
            )
        known_tasks = {str(task["task_id"]) for task in self.db.list_tasks()}
        stages: List[Dict[str, Any]] = []
        seen_stage_ids: set[str] = set()
        seen_item_ids: set[str] = set()
        item_count = 0
        for stage in raw_stages:
            if not isinstance(stage, Mapping):
                raise ConveyorValidationError("each stage must be an object")
            stage_id = str(stage.get("stage_id") or "").strip()
            if not _ID_RE.fullmatch(stage_id) or stage_id in seen_stage_ids:
                raise ConveyorValidationError("stage_id must be unique and URL-safe")
            seen_stage_ids.add(stage_id)
            raw_items = stage.get("items")
            if not isinstance(raw_items, list) or not raw_items:
                raise ConveyorValidationError("each stage must contain at least one task")
            stage_items: List[Dict[str, str]] = []
            stage_task_ids: set[str] = set()
            for item in raw_items:
                if not isinstance(item, Mapping):
                    raise ConveyorValidationError("each conveyor item must be an object")
                item_id = str(item.get("item_id") or "").strip()
                task_id = str(item.get("task_id") or "").strip()
                if not _ID_RE.fullmatch(item_id) or item_id in seen_item_ids:
                    raise ConveyorValidationError("item_id must be globally unique and URL-safe")
                if task_id not in known_tasks:
                    raise ConveyorValidationError(f"Unknown task id: {task_id}")
                if task_id in stage_task_ids:
                    raise ConveyorValidationError(
                        f"Task {task_id} cannot appear twice in one parallel row"
                    )
                seen_item_ids.add(item_id)
                stage_task_ids.add(task_id)
                stage_items.append({"item_id": item_id, "task_id": task_id})
                item_count += 1
                if item_count > self.MAX_ITEMS:
                    raise ConveyorValidationError(
                        f"conveyor must contain at most {self.MAX_ITEMS} tasks"
                    )
            stages.append({"stage_id": stage_id, "items": stage_items})
        return stages

    @staticmethod
    def _flatten_definition(stages: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for stage_order, stage in enumerate(stages):
            for task_order, item in enumerate(stage.get("items") or []):
                result[str(item["item_id"])] = {
                    "stage_id": str(stage["stage_id"]),
                    "stage_order": stage_order,
                    "task_order": task_order,
                    "task_id": str(item["task_id"]),
                }
        return result

    def save_definition(
        self,
        *,
        expected_revision: int,
        stages: Any,
    ) -> Dict[str, Any]:
        normalized = self.normalize_stages(stages)
        active = self.db.get_active_conveyor_run()
        if active:
            run_id = int(active["conveyor_run_id"])
            proposed = self._flatten_definition(normalized)
            run_items = self.db.list_conveyor_run_items(run_id)
            locked_ids: set[str] = set()
            locked_stage_order = -1
            for item in run_items:
                if str(item.get("status")) == "pending":
                    continue
                item_id = str(item["item_id"])
                locked_ids.add(item_id)
                locked_stage_order = max(locked_stage_order, int(item["stage_order"]))
                candidate = proposed.get(item_id)
                expected = {
                    "stage_id": str(item["stage_id"]),
                    "stage_order": int(item["stage_order"]),
                    "task_order": int(item["task_order"]),
                    "task_id": str(item["task_id"]),
                }
                if candidate != expected:
                    raise ConveyorEditConflict(
                        "Completed and currently running conveyor rows are locked"
                    )
            if any(
                item_id not in locked_ids
                and int(item["stage_order"]) <= locked_stage_order
                for item_id, item in proposed.items()
            ):
                raise ConveyorEditConflict(
                    "New tasks may only be added after the current conveyor row"
                )
        definition = self.db.save_conveyor_definition(
            expected_revision=int(expected_revision),
            stages=normalized,
        )
        self.db.insert_event(
            "conveyor.updated",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"revision": definition["revision"], "stages": normalized},
        )
        return definition

    def snapshot(self) -> Dict[str, Any]:
        get_snapshot = getattr(self.db, "get_conveyor_snapshot", None)
        if get_snapshot is not None:
            return get_snapshot()
        definition = self.db.get_conveyor_definition()
        active = self.db.get_active_conveyor_run()
        latest = active or self.db.get_latest_conveyor_run()
        items = (
            self.db.list_conveyor_run_items(int(latest["conveyor_run_id"]))
            if latest
            else []
        )
        return {
            "definition": definition,
            "run": latest,
            "items": items,
        }

    def trigger(self, *, sudo_password: Optional[str] = None) -> Dict[str, Any]:
        with self._trigger_lock:
            definition = self.db.get_conveyor_definition()
            stages = definition.get("stages") or []
            if not stages:
                return {"action": "noop", "reason": "conveyor_empty", "message": "Add tasks first."}
            active = self.db.get_active_conveyor_run()
            if active:
                return {"action": "noop", "reason": "already_running", "run": active}

            for stage in stages:
                for item in stage.get("items") or []:
                    task_id = str(item["task_id"])
                    task_active = self.db.get_active_run_for_task(task_id)
                    if task_active:
                        return {
                            "action": "noop",
                            "reason": "task_already_running",
                            "message": f"Task {task_id} is already running.",
                            "task_id": task_id,
                        }
                    preflight = self.runner.check_task_start(
                        task_id,
                        sudo_password=sudo_password,
                    )
                    if not preflight.get("ok", False):
                        reason = str(preflight.get("reason") or "task_not_started")
                        return {
                            "action": reason if reason.startswith("sudo_") else "noop",
                            "reason": reason,
                            "message": preflight.get("message"),
                            "task_id": task_id,
                        }

            run_id = self.db.create_conveyor_run()
        self.db.insert_event(
            "conveyor.started",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"conveyor_run_id": run_id},
        )
        thread = threading.Thread(
            target=self._run_and_clear,
            args=(run_id, sudo_password),
            name=f"conveyor-run-{run_id}",
            daemon=True,
        )
        with self._thread_lock:
            self._thread = thread
        thread.start()
        return {"action": "start", "run": self.db.get_conveyor_run(run_id)}

    def shutdown(self, *, timeout_seconds: float = 10.0) -> None:
        """Request a safe conveyor stop and wait before database shutdown."""
        with self._thread_lock:
            thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self.stop()
        thread.join(timeout=max(0.0, float(timeout_seconds)))

    def _run_and_clear(
        self,
        conveyor_run_id: int,
        sudo_password: Optional[str],
    ) -> None:
        try:
            self._run(conveyor_run_id, sudo_password)
        finally:
            with self._thread_lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def stop(self) -> Dict[str, Any]:
        active = self.db.get_active_conveyor_run()
        if not active:
            return {"action": "noop", "reason": "not_running"}
        run_id = int(active["conveyor_run_id"])
        self.db.request_conveyor_stop(run_id)
        affected: List[int] = []
        for item in self.db.list_conveyor_run_items(run_id):
            task_run_id = item.get("task_run_id")
            if str(item.get("status")) != "running" or task_run_id is None:
                continue
            affected.append(int(task_run_id))
            self.runner.request_stop_run(int(task_run_id), mode="graceful")
        self.db.insert_event(
            "conveyor.stop_requested",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"conveyor_run_id": run_id, "task_run_ids": affected},
        )
        return {"action": "stop_graceful", "run": self.db.get_conveyor_run(run_id)}

    def _run(self, conveyor_run_id: int, sudo_password: Optional[str]) -> None:
        self.db.set_conveyor_run_running(conveyor_run_id)
        try:
            while True:
                run = self.db.get_conveyor_run(conveyor_run_id)
                if not run:
                    return
                if run.get("stop_requested"):
                    self._finish_run(conveyor_run_id, status=CONVEYOR_RUN_STATUS_STOPPED)
                    return
                stage = self.db.claim_next_conveyor_stage(conveyor_run_id)
                if not stage:
                    self._finish_run(
                        conveyor_run_id,
                        status=CONVEYOR_RUN_STATUS_COMPLETED,
                        outcome="completed",
                    )
                    return
                stage_result = self._run_stage(
                    conveyor_run_id,
                    stage,
                    sudo_password=sudo_password,
                )
                if stage_result == "failed":
                    self._finish_run(
                        conveyor_run_id,
                        status=CONVEYOR_RUN_STATUS_FAILED,
                        error_text="A conveyor task failed.",
                    )
                    return
                if stage_result == "stopped":
                    self._finish_run(conveyor_run_id, status=CONVEYOR_RUN_STATUS_STOPPED)
                    return
                if stage_result == "no_op":
                    self.db.request_conveyor_stop(conveyor_run_id)
                    self._finish_run(
                        conveyor_run_id,
                        status=CONVEYOR_RUN_STATUS_COMPLETED,
                        outcome="no_op",
                    )
                    return
        except Exception as exc:  # pragma: no cover - defensive boundary
            self._finish_run(
                conveyor_run_id,
                status=CONVEYOR_RUN_STATUS_FAILED,
                error_text=f"{type(exc).__name__}: {exc}",
            )

    def _run_stage(
        self,
        conveyor_run_id: int,
        stage: List[Dict[str, Any]],
        *,
        sudo_password: Optional[str],
    ) -> str:
        stage_order = int(stage[0]["stage_order"])
        self.db.insert_event(
            "conveyor.stage_started",
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={"conveyor_run_id": conveyor_run_id, "stage_order": stage_order},
        )
        active: Dict[str, int] = {}
        failed_to_start = False
        for item in stage:
            task_id = str(item["task_id"])
            started = self.runner.start_task(task_id, sudo_password=sudo_password)
            if started.get("action") != "start":
                failed_to_start = True
                reason = str(started.get("reason") or "task_not_started")
                self.db.finish_conveyor_item(
                    conveyor_run_id,
                    str(item["item_id"]),
                    status=TASK_RUN_STATUS_FAILED,
                    meaningful=None,
                    output={"reason": reason},
                    error_text=f"Task did not start: {reason}",
                )
                continue
            task_run_id = int(started["run"]["run_id"])
            active[str(item["item_id"])] = task_run_id
            self.db.set_conveyor_item_running(
                conveyor_run_id,
                str(item["item_id"]),
                task_run_id,
            )
            self.db.insert_event(
                "conveyor.task_started",
                task_id=task_id,
                run_id=task_run_id,
                panel_id=None,
                payload={
                    "conveyor_run_id": conveyor_run_id,
                    "item_id": item["item_id"],
                    "stage_order": stage_order,
                },
            )

        terminal: Dict[str, Dict[str, Any]] = {}
        while active:
            run = self.db.get_conveyor_run(conveyor_run_id) or {}
            if run.get("stop_requested"):
                for task_run_id in active.values():
                    task_run = self.db.get_run(task_run_id) or {}
                    if task_run.get("status") in TASK_RUN_ACTIVE_STATUSES:
                        self.runner.request_stop_run(task_run_id, mode="graceful")
            for item_id, task_run_id in list(active.items()):
                task_run = self.db.get_run(task_run_id)
                if task_run and task_run.get("status") not in TASK_RUN_ACTIVE_STATUSES:
                    terminal[item_id] = task_run
                    del active[item_id]
            if active:
                time.sleep(self.poll_seconds)

        any_failed = failed_to_start
        any_stopped = False
        meaningful_values: List[bool] = []
        for item in stage:
            item_id = str(item["item_id"])
            task_run = terminal.get(item_id)
            if not task_run:
                continue
            status = str(task_run.get("status") or TASK_RUN_STATUS_FAILED)
            meaningful = (
                self._is_meaningful(str(item["task_id"]), task_run)
                if status == TASK_RUN_STATUS_COMPLETED
                else None
            )
            if meaningful is not None:
                meaningful_values.append(meaningful)
            output = self._task_output(task_run)
            self.db.finish_conveyor_item(
                conveyor_run_id,
                item_id,
                status=status,
                meaningful=meaningful,
                output=output,
                error_text=task_run.get("error_text"),
            )
            any_failed = any_failed or status == TASK_RUN_STATUS_FAILED
            any_stopped = any_stopped or status == TASK_RUN_STATUS_STOPPED
            self.db.insert_event(
                "conveyor.task_finished",
                task_id=str(item["task_id"]),
                run_id=int(task_run["run_id"]),
                panel_id=None,
                payload={
                    "conveyor_run_id": conveyor_run_id,
                    "item_id": item_id,
                    "stage_order": stage_order,
                    "status": status,
                    "meaningful": meaningful,
                },
            )
        if any_failed:
            return "failed"
        if any_stopped:
            return "stopped"
        if len(stage) == 1 and meaningful_values == [False]:
            return "no_op"
        return "completed"

    def _is_meaningful(self, task_id: str, task_run: Mapping[str, Any]) -> bool:
        task = self.db.get_task(task_id) or {}
        policy = task.get("meaningful_result")
        if not isinstance(policy, Mapping) or not policy:
            return True
        output = self._task_output(task_run)
        expected_kind = str(policy.get("artifact_kind") or "").strip()
        if expected_kind and str(output.get("kind") or "") != expected_kind:
            return False
        fields = policy.get("any_positive")
        if not isinstance(fields, list) or not fields:
            return True
        for field in fields:
            try:
                if float(output.get(str(field)) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def _task_output(task_run: Mapping[str, Any]) -> Dict[str, Any]:
        summary = task_run.get("summary")
        if not isinstance(summary, Mapping):
            return {}
        artifacts = summary.get("artifacts")
        return dict(artifacts) if isinstance(artifacts, Mapping) else {}

    def _finish_run(
        self,
        conveyor_run_id: int,
        *,
        status: str,
        outcome: Optional[str] = None,
        error_text: Optional[str] = None,
    ) -> None:
        self.db.finish_conveyor_run(
            conveyor_run_id,
            status=status,
            outcome=outcome,
            error_text=error_text,
        )
        event = {
            CONVEYOR_RUN_STATUS_COMPLETED: "conveyor.completed",
            CONVEYOR_RUN_STATUS_FAILED: "conveyor.failed",
            CONVEYOR_RUN_STATUS_STOPPED: "conveyor.stopped",
        }.get(status, "conveyor.finished")
        self.db.insert_event(
            event,
            task_id=None,
            run_id=None,
            panel_id=None,
            payload={
                "conveyor_run_id": conveyor_run_id,
                "status": status,
                "outcome": outcome,
                "error": error_text,
            },
        )


__all__ = [
    "ConveyorEditConflict",
    "ConveyorRevisionConflict",
    "ConveyorService",
    "ConveyorValidationError",
]
