"""Runtime and validation tests for the editable task conveyor."""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.conveyor import ConveyorEditConflict, ConveyorService, ConveyorValidationError


class _Db:
    def __init__(self) -> None:
        self.tasks = {
            "a": {"task_id": "a", "meaningful_result": {}},
            "b": {"task_id": "b", "meaningful_result": {}},
        }
        self.definition = {"revision": 0, "stages": []}
        self.items: list[dict] = []

    def list_tasks(self):
        return list(self.tasks.values())

    def get_task(self, task_id):  # noqa: ANN001
        return self.tasks.get(task_id)

    def get_active_conveyor_run(self):
        return {"conveyor_run_id": 4, "status": "running"} if self.items else None

    def list_conveyor_run_items(self, _run_id):  # noqa: ANN001
        return deepcopy(self.items)

    def save_conveyor_definition(self, *, expected_revision, stages):  # noqa: ANN001
        assert expected_revision == self.definition["revision"]
        self.definition = {"revision": expected_revision + 1, "stages": deepcopy(stages)}
        return deepcopy(self.definition)

    def insert_event(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return 1


class _Runner:
    pass


class _StageDb(_Db):
    def __init__(self, task_runs: dict[int, dict]) -> None:
        super().__init__()
        self.task_runs = task_runs
        self.finished: list[dict] = []
        self.running: list[tuple[str, int]] = []

    def set_conveyor_item_running(self, _run_id, item_id, task_run_id):  # noqa: ANN001
        self.running.append((str(item_id), int(task_run_id)))

    def get_conveyor_run(self, _run_id):  # noqa: ANN001
        return {"stop_requested": False}

    def get_run(self, task_run_id):  # noqa: ANN001
        return self.task_runs[int(task_run_id)]

    def finish_conveyor_item(self, _run_id, item_id, **values):  # noqa: ANN001, ANN003
        self.finished.append({"item_id": item_id, **values})


class _StageRunner:
    def __init__(self, run_ids: dict[str, int]) -> None:
        self.run_ids = run_ids
        self.started: list[str] = []

    def start_task(self, task_id, *, sudo_password=None):  # noqa: ANN001
        _ = sudo_password
        self.started.append(str(task_id))
        return {"action": "start", "run": {"run_id": self.run_ids[str(task_id)]}}


def test_conveyor_validates_rows_and_duplicate_parallel_tasks() -> None:
    service = ConveyorService(_Db(), _Runner())
    normalized = service.normalize_stages(
        [
            {
                "stage_id": "stage-1",
                "items": [
                    {"item_id": "item-1", "task_id": "a"},
                    {"item_id": "item-2", "task_id": "b"},
                ],
            }
        ]
    )
    assert [item["task_id"] for item in normalized[0]["items"]] == ["a", "b"]

    with pytest.raises(ConveyorValidationError, match="cannot appear twice"):
        service.normalize_stages(
            [
                {
                    "stage_id": "stage-1",
                    "items": [
                        {"item_id": "item-1", "task_id": "a"},
                        {"item_id": "item-2", "task_id": "a"},
                    ],
                }
            ]
        )


def test_live_edit_preserves_locked_rows_and_allows_future_rows() -> None:
    db = _Db()
    db.items = [
        {
            "item_id": "item-1",
            "stage_id": "stage-1",
            "stage_order": 0,
            "task_order": 0,
            "task_id": "a",
            "status": "running",
        },
        {
            "item_id": "item-2",
            "stage_id": "stage-2",
            "stage_order": 1,
            "task_order": 0,
            "task_id": "b",
            "status": "pending",
        },
    ]
    service = ConveyorService(db, _Runner())

    saved = service.save_definition(
        expected_revision=0,
        stages=[
            {"stage_id": "stage-1", "items": [{"item_id": "item-1", "task_id": "a"}]},
            {"stage_id": "stage-3", "items": [{"item_id": "item-3", "task_id": "b"}]},
        ],
    )
    assert saved["revision"] == 1

    with pytest.raises(ConveyorEditConflict, match="locked"):
        service.save_definition(
            expected_revision=1,
            stages=[
                {"stage_id": "stage-1", "items": [{"item_id": "item-1", "task_id": "b"}]},
            ],
        )


def test_meaningful_policy_uses_structured_artifacts() -> None:
    db = _Db()
    db.tasks["a"]["meaningful_result"] = {
        "artifact_kind": "scan",
        "any_positive": ["added", "changed"],
    }
    service = ConveyorService(db, _Runner())

    assert service._is_meaningful(
        "a",
        {"summary": {"artifacts": {"kind": "scan", "added": 0, "changed": 1}}},
    ) is True
    assert service._is_meaningful(
        "a",
        {"summary": {"artifacts": {"kind": "scan", "added": 0, "changed": 0}}},
    ) is False


def test_parallel_stage_finishes_siblings_before_reporting_failure() -> None:
    db = _StageDb(
        {
            1: {"run_id": 1, "status": "failed", "summary": {}, "error_text": "boom"},
            2: {"run_id": 2, "status": "completed", "summary": {}},
        }
    )
    runner = _StageRunner({"a": 1, "b": 2})
    service = ConveyorService(db, runner, poll_seconds=0)
    result = service._run_stage(
        7,
        [
            {"stage_order": 0, "item_id": "item-a", "task_id": "a"},
            {"stage_order": 0, "item_id": "item-b", "task_id": "b"},
        ],
        sudo_password=None,
    )

    assert result == "failed"
    assert runner.started == ["a", "b"]
    assert {item["status"] for item in db.finished} == {"failed", "completed"}


def test_no_op_stops_single_stage_but_is_ignored_in_parallel_stage() -> None:
    completed_noop = {
        "run_id": 1,
        "status": "completed",
        "summary": {"artifacts": {"kind": "scan", "added": 0}},
    }
    db = _StageDb({1: completed_noop, 2: {**completed_noop, "run_id": 2}})
    db.tasks["a"]["meaningful_result"] = {
        "artifact_kind": "scan",
        "any_positive": ["added"],
    }
    db.tasks["b"]["meaningful_result"] = {
        "artifact_kind": "scan",
        "any_positive": ["added"],
    }
    service = ConveyorService(db, _StageRunner({"a": 1, "b": 2}), poll_seconds=0)

    assert service._run_stage(
        8,
        [{"stage_order": 0, "item_id": "item-a", "task_id": "a"}],
        sudo_password=None,
    ) == "no_op"
    assert service._run_stage(
        9,
        [
            {"stage_order": 0, "item_id": "item-a", "task_id": "a"},
            {"stage_order": 0, "item_id": "item-b", "task_id": "b"},
        ],
        sudo_password=None,
    ) == "completed"
