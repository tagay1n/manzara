"""API/runtime behavior tests for Manzara."""

from __future__ import annotations

import asyncio
import json
import time

from app.modules.maintenance.workflow import LIBRARY_WORKFLOW_ID
from app.modules.shayan.workflow import SHAYAN_WEEKLY_SCHEDULE_ID, SHAYAN_WEEKLY_WORKFLOW_ID


def _wait_for_status(main_app, run_id: int, expected: set[str], timeout_seconds: float = 4.0):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        run = main_app.state.db.get_run(run_id)
        if run and run["status"] in expected:
            return run
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not reach expected status: {expected}")


def test_dashboard_lists_shayan_tasks(test_client) -> None:
    client, _main_app = test_client

    response = client.get("/api/dashboard")
    assert response.status_code == 200
    payload = response.json()

    panels = {panel["panel_id"]: panel for panel in payload["panels"]}
    assert "shayan" in panels
    assert "maintenance" in panels

    shayan = panels["shayan"]
    task_ids = {task["task_id"] for task in shayan["tasks"]}
    assert {"shayan.quick", "shayan.long", "shayan.ignore_sigint"} <= task_ids
    assert {"shayan.scan_changes", "shayan.download_new"} <= task_ids
    assert shayan["workflows"][0]["workflow_id"] == SHAYAN_WEEKLY_WORKFLOW_ID

    maintenance = panels["maintenance"]
    maintenance_task_ids = {task["task_id"] for task in maintenance["tasks"]}
    assert "maintenance.monocorpus_sync" in maintenance_task_ids
    assert "maintenance.monocorpus_meta_evaluate" not in maintenance_task_ids

    library = panels["library"]
    library_task_ids = {task["task_id"] for task in library["tasks"]}
    assert "maintenance.monocorpus_meta_evaluate" in library_task_ids


def test_rename_flow_and_task_title(test_client) -> None:
    client, main_app = test_client

    flow_resp = client.patch("/api/flows/shayan/title", json={"title": "Shayan Flow"})
    assert flow_resp.status_code == 200
    assert flow_resp.json()["updated"] is True
    assert flow_resp.json()["flow"]["title"] == "Shayan Flow"

    task_resp = client.patch("/api/tasks/shayan.quick/title", json={"title": "Quick Runner"})
    assert task_resp.status_code == 200
    assert task_resp.json()["updated"] is True
    assert task_resp.json()["task"]["title"] == "Quick Runner"

    payload = client.get("/api/dashboard").json()
    panels = {panel["panel_id"]: panel for panel in payload["panels"]}
    assert panels["shayan"]["title"] == "Shayan Flow"
    quick_task = next(task for task in panels["shayan"]["tasks"] if task["task_id"] == "shayan.quick")
    assert quick_task["title"] == "Quick Runner"

    # Simulate startup reseeding and verify user-renamed labels remain persisted.
    main_app.state.db.seed_panels(main_app._PANEL_DEFS)
    main_app.state.db.seed_tasks(main_app.shayan_task_definitions(main_app.state.settings.shayan))
    main_app.state.db.seed_tasks(main_app.maintenance_task_definitions(main_app.state.settings.maintenance))

    payload_after_seed = client.get("/api/dashboard").json()
    panels_after_seed = {panel["panel_id"]: panel for panel in payload_after_seed["panels"]}
    assert panels_after_seed["shayan"]["title"] == "Shayan Flow"
    quick_task_after_seed = next(
        task for task in panels_after_seed["shayan"]["tasks"] if task["task_id"] == "shayan.quick"
    )
    assert quick_task_after_seed["title"] == "Quick Runner"


def test_update_schedule_and_recompute_next_run(test_client) -> None:
    client, _main_app = test_client

    response = client.patch(
        f"/api/schedules/{SHAYAN_WEEKLY_SCHEDULE_ID}",
        json={"enabled": True, "day_of_week": 5, "time_of_day": "10:45"},
    )
    assert response.status_code == 200
    schedule = response.json()["schedule"]
    assert schedule["enabled"] is True
    assert int(schedule["day_of_week"]) == 5
    assert schedule["time_of_day"] == "10:45"
    assert schedule["next_run_at"] is not None


def test_schedules_endpoint_returns_workflows(test_client) -> None:
    client, _main_app = test_client

    response = client.get("/api/schedules")
    assert response.status_code == 200
    payload = response.json()
    assert "workflows" in payload
    workflow_ids = {item["workflow_id"] for item in payload["workflows"]}
    assert SHAYAN_WEEKLY_WORKFLOW_ID in workflow_ids
    assert LIBRARY_WORKFLOW_ID in workflow_ids


def test_tasks_endpoint_groups_tasks_by_flow(test_client) -> None:
    client, _main_app = test_client

    response = client.get("/api/tasks")
    assert response.status_code == 200
    payload = response.json()
    flow_ids = {flow["panel_id"] for flow in payload["flows"]}
    assert {"shayan", "maintenance", "library"} <= flow_ids

    shayan = next(item for item in payload["flows"] if item["panel_id"] == "shayan")
    task_ids = {task["task_id"] for task in shayan["tasks"]}
    assert {"shayan.scan_changes", "shayan.download_new"} <= task_ids
    assert any(task["task_id"] == "shayan.quick" and task["slug"] == "quick" for task in shayan["tasks"])


def test_task_detail_endpoint_returns_run_history(test_client, wait_for_terminal_run) -> None:
    client, main_app = test_client

    response = client.post("/api/tasks/shayan.quick/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "completed"

    detail = client.get("/api/tasks/shayan.quick?limit=10")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["task"]["task_id"] == "shayan.quick"
    assert payload["panel"]["panel_id"] == "shayan"
    assert payload["stats"]["total_runs"] >= 1
    assert len(payload["runs"]) >= 1
    assert payload["runs"][0]["task_id"] == "shayan.quick"


def test_task_detail_endpoint_accepts_human_slug(test_client) -> None:
    client, _main_app = test_client

    payload = client.get("/api/tasks/quick").json()
    assert payload["task"]["task_id"] == "shayan.quick"
    assert payload["task"]["slug"] == "quick"


def test_workflow_run_skips_download_when_no_new(
    test_client,
    wait_for_terminal_workflow_run,
) -> None:
    client, main_app = test_client

    response = client.post(f"/api/workflows/{SHAYAN_WEEKLY_WORKFLOW_ID}/run")
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "start"

    workflow_run_id = int(payload["workflow_run"]["workflow_run_id"])
    workflow_run = wait_for_terminal_workflow_run(main_app, workflow_run_id)
    assert workflow_run["status"] == "completed"
    assert int(workflow_run["context"].get("scan_new_items_count", -1)) == 0

    step_runs = main_app.state.db.list_workflow_step_runs(workflow_run_id)
    assert len(step_runs) == 2
    assert step_runs[0]["task_id"] == "shayan.scan_changes"
    assert step_runs[0]["status"] == "completed"
    assert step_runs[1]["task_id"] == "shayan.download_new"
    assert step_runs[1]["status"] == "skipped"


def test_toggle_task_start_and_complete(test_client, wait_for_terminal_run) -> None:
    client, main_app = test_client

    response = client.post("/api/tasks/shayan.quick/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])

    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "completed"

    logs = client.get(f"/api/runs/{run_id}/logs").json()["lines"]
    assert any("quick-ok" in line["line"] for line in logs)


def test_toggle_task_graceful_then_force(test_client, wait_for_terminal_run) -> None:
    client, main_app = test_client

    started = client.post("/api/tasks/shayan.ignore_sigint/toggle").json()
    run_id = int(started["run"]["run_id"])
    _wait_for_status(main_app, run_id, {"running"})

    graceful = client.post("/api/tasks/shayan.ignore_sigint/toggle")
    assert graceful.status_code == 200
    assert graceful.json()["action"] == "stop_graceful"

    force = client.post("/api/tasks/shayan.ignore_sigint/toggle")
    assert force.status_code == 200
    assert force.json()["action"] in {"stop_force", "noop"}

    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "stopped"


def test_stop_all_two_step_force(test_client, wait_for_terminal_run) -> None:
    client, main_app = test_client

    started = client.post("/api/tasks/shayan.ignore_sigint/toggle").json()
    run_id = int(started["run"]["run_id"])
    _wait_for_status(main_app, run_id, {"running"})

    first = client.post("/api/system/stop-all")
    assert first.status_code == 200
    assert first.json()["action"] == "stop_all_graceful"

    second = client.post("/api/system/stop-all")
    assert second.status_code == 200
    assert second.json()["action"] in {"stop_all_force", "noop"}

    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "stopped"


def test_events_stream_outputs_sse_frames(test_client) -> None:
    client, main_app = test_client

    event = main_app.state.db.insert_event(
        "task.started",
        task_id="shayan.quick",
        run_id=1,
        panel_id="shayan",
        payload={"status": "starting"},
    )

    class _FakeRequest:
        headers = {}

        async def is_disconnected(self) -> bool:
            return False

    async def _read_first_chunk():
        response = await main_app.events_stream(_FakeRequest(), after_event_id=0)
        assert response.media_type == "text/event-stream"
        iterator = response.body_iterator
        chunk = await anext(iterator)
        if hasattr(iterator, "aclose"):
            await iterator.aclose()
        return chunk

    chunk = asyncio.run(_read_first_chunk())
    text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
    assert text.startswith(f"id: {event['event_id']}")
    assert "\nevent: task.started\n" in text
    assert "\ndata: " in text

    payload_line = [line for line in text.splitlines() if line.startswith("data: ")][0]
    payload = json.loads(payload_line.replace("data: ", "", 1))
    assert payload["type"].startswith("task.") or payload["type"].startswith("system.")
