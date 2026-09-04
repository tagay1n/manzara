"""API/runtime behavior tests for Manzara."""

from __future__ import annotations

import time



def _wait_for_status(main_app, run_id: int, expected: set[str], timeout_seconds: float = 4.0):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        run = main_app.state.db.get_run(run_id)
        if run and run["status"] in expected:
            return run
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not reach expected status: {expected}")


def test_root_redirects_to_tasks_page(test_client) -> None:
    client, _main_app = test_client
    response = client.get("/", follow_redirects=False)
    assert response.status_code in {302, 307, 308}
    assert response.headers["location"] == "/tasks"


def test_dashboard_page_redirects_to_tasks_page(test_client) -> None:
    client, _main_app = test_client
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code in {302, 307, 308}
    assert response.headers["location"] == "/tasks"


def test_flow_catalog_page_and_detail_api_are_removed(test_client) -> None:
    client, _main_app = test_client

    assert client.get("/flows/retired").status_code == 404
    assert client.get("/api/flows/retired").status_code == 404


def test_html_pages_disable_browser_caching(test_client) -> None:
    client, _main_app = test_client

    for path in ("/tasks", "/tasks/maintenance.scan_test"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


def test_static_ui_assets_require_browser_revalidation(test_client) -> None:
    client, _main_app = test_client

    for path in ("/static/styles.css", "/static/shell.js", "/static/shell-state.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"


def test_tasks_catalog_includes_global_conveyor_and_can_save_definition(test_client) -> None:
    client, _main_app = test_client

    catalog = client.get("/api/tasks")
    assert catalog.status_code == 200
    conveyor = catalog.json()["conveyor"]
    assert conveyor["definition"]["revision"] == 0
    assert any(task["task_id"] == "maintenance.quick" for task in conveyor["available_tasks"])

    response = client.put(
        "/api/conveyor",
        json={
            "revision": 0,
            "stages": [
                {
                    "stage_id": "stage-1",
                    "items": [
                        {"item_id": "item-1", "task_id": "maintenance.quick"},
                    ],
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["definition"]["revision"] == 1

    stale = client.put("/api/conveyor", json={"revision": 0, "stages": []})
    assert stale.status_code == 409


def test_conveyor_rejects_non_integral_revision_and_unknown_task(test_client) -> None:
    client, _main_app = test_client

    fractional = client.put("/api/conveyor", json={"revision": 0.5, "stages": []})
    assert fractional.status_code == 400

    unknown = client.put(
        "/api/conveyor",
        json={
            "revision": 0,
            "stages": [
                {
                    "stage_id": "stage-1",
                    "items": [{"item_id": "item-1", "task_id": "missing.task"}],
                }
            ],
        },
    )
    assert unknown.status_code == 400


def test_conveyor_runs_sequential_rows_to_completion(test_client) -> None:
    client, main_app = test_client
    saved = client.put(
        "/api/conveyor",
        json={
            "revision": 0,
            "stages": [
                {
                    "stage_id": "stage-1",
                    "items": [{"item_id": "item-1", "task_id": "maintenance.quick"}],
                },
                {
                    "stage_id": "stage-2",
                    "items": [{"item_id": "item-2", "task_id": "maintenance.scan_test"}],
                },
            ],
        },
    )
    assert saved.status_code == 200

    started = client.post("/api/conveyor/run", json={})
    assert started.status_code == 200
    run_id = int(started.json()["run"]["conveyor_run_id"])
    deadline = time.time() + 20
    run = None
    while time.time() < deadline:
        run = main_app.state.db.get_conveyor_run(run_id)
        if run and run["status"] not in {"starting", "running"}:
            break
        time.sleep(0.05)

    assert run is not None
    assert run["status"] == "completed"
    items = main_app.state.db.list_conveyor_run_items(run_id)
    assert [item["status"] for item in items] == ["completed", "completed"]
    assert items[0]["task_run_id"] < items[1]["task_run_id"]


def test_system_state_returns_lightweight_global_payload(test_client) -> None:
    client, _main_app = test_client

    response = client.get("/api/system/state")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload.get("generated_at"), str)
    assert isinstance(payload.get("event_cursor"), int)
    assert set(payload["global"]) >= {
        "active_tasks",
        "stop_all_state",
    }


def test_dashboard_lists_operational_tasks(test_client) -> None:
    client, _main_app = test_client

    response = client.get("/api/dashboard")
    assert response.status_code == 200
    payload = response.json()

    panels = {panel["panel_id"]: panel for panel in payload["panels"]}
    assert "maintenance" in panels
    assert "backup" in panels

    maintenance = panels["maintenance"]
    maintenance_task_ids = {task["task_id"] for task in maintenance["tasks"]}
    assert {"maintenance.quick", "maintenance.long", "maintenance.ignore_sigint"} <= maintenance_task_ids
    assert {"maintenance.scan_test", "maintenance.download_test"} <= maintenance_task_ids
    assert "maintenance.monocorpus_sync" in maintenance_task_ids
    assert "maintenance.pgbackrest_backup_full" not in maintenance_task_ids
    assert "maintenance.pgbackrest_backup_incr" not in maintenance_task_ids
    assert "maintenance.monocorpus_meta_evaluate" not in maintenance_task_ids

    backup = panels["backup"]
    backup_tasks = {task["task_id"]: task for task in backup["tasks"]}
    assert backup_tasks["maintenance.pgbackrest_backup_full"]["title"] == "Full backup"
    assert backup_tasks["maintenance.pgbackrest_backup_incr"]["title"] == "Incremental backup"

    library = panels["library"]
    library_task_ids = {task["task_id"] for task in library["tasks"]}
    assert "maintenance.monocorpus_meta_evaluate" not in library_task_ids
    assert "library.metadata_extract" not in library_task_ids
    assert "library.metadata_validate" not in library_task_ids
    assert not any(task_id.startswith("library.collection_") for task_id in library_task_ids)

    metadata = panels["metadata"]
    metadata_task_ids = {task["task_id"] for task in metadata["tasks"]}
    assert {
        "maintenance.monocorpus_meta_evaluate",
        "library.metadata_extract",
        "library.metadata_validate",
    } <= metadata_task_ids

    collections = panels["collections"]
    collection_task_ids = {task["task_id"] for task in collections["tasks"]}
    assert collection_task_ids == {
        "library.collection_detect",
        "library.collection_validate",
        "library.collection_apply",
    }


def test_rename_flow_and_task_title(test_client) -> None:
    client, main_app = test_client

    flow_resp = client.patch("/api/flows/maintenance/title", json={"title": "Operations"})
    assert flow_resp.status_code == 200
    assert flow_resp.json()["updated"] is True
    assert flow_resp.json()["flow"]["title"] == "Operations"

    task_resp = client.patch("/api/tasks/maintenance.quick/title", json={"title": "Quick Runner"})
    assert task_resp.status_code == 200
    assert task_resp.json()["updated"] is True
    assert task_resp.json()["task"]["title"] == "Quick Runner"

    payload = client.get("/api/dashboard").json()
    panels = {panel["panel_id"]: panel for panel in payload["panels"]}
    assert panels["maintenance"]["title"] == "Operations"
    quick_task = next(task for task in panels["maintenance"]["tasks"] if task["task_id"] == "maintenance.quick")
    assert quick_task["title"] == "Quick Runner"

    # Simulate startup reseeding and verify user-renamed labels remain persisted.
    main_app.state.db.seed_panels(main_app._PANEL_DEFS)
    main_app.state.db.seed_tasks(main_app.maintenance_task_definitions(main_app.state.settings.maintenance))

    payload_after_seed = client.get("/api/dashboard").json()
    panels_after_seed = {panel["panel_id"]: panel for panel in payload_after_seed["panels"]}
    assert panels_after_seed["maintenance"]["title"] == "Operations"
    quick_task_after_seed = next(
        task for task in panels_after_seed["maintenance"]["tasks"] if task["task_id"] == "maintenance.quick"
    )
    assert quick_task_after_seed["title"] == "Quick Runner"


def test_tasks_endpoint_groups_tasks_by_flow(test_client) -> None:
    client, main_app = test_client

    event = main_app.state.db.insert_event(
        "task.started",
        task_id="maintenance.quick",
        run_id=999,
        panel_id="maintenance",
        payload={"status": "starting"},
    )

    response = client.get("/api/tasks")
    assert response.status_code == 200
    payload = response.json()
    assert payload["event_cursor"] == event["event_id"]
    flow_ids = {flow["panel_id"] for flow in payload["flows"]}
    assert {"maintenance", "library"} <= flow_ids

    maintenance = next(item for item in payload["flows"] if item["panel_id"] == "maintenance")
    task_ids = {task["task_id"] for task in maintenance["tasks"]}
    assert {"maintenance.scan_test", "maintenance.download_test"} <= task_ids
    assert any(task["task_id"] == "maintenance.quick" and task["slug"] == "quick" for task in maintenance["tasks"])


def test_task_detail_endpoint_returns_run_history(test_client, wait_for_terminal_run) -> None:
    client, main_app = test_client

    response = client.post("/api/tasks/maintenance.quick/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "completed"

    detail = client.get("/api/tasks/maintenance.quick?limit=10")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["task"]["task_id"] == "maintenance.quick"
    assert payload["panel"]["panel_id"] == "maintenance"
    assert payload["stats"]["total_runs"] >= 1
    assert len(payload["runs"]) >= 1
    assert payload["runs"][0]["task_id"] == "maintenance.quick"


def test_task_detail_default_limit_is_twenty(test_client) -> None:
    client, main_app = test_client
    db = main_app.state.db
    task = db.get_task("maintenance.quick")
    assert task is not None

    for idx in range(25):
        run_id = db.create_run(task)
        db.mark_run_started(run_id, pid=20000 + idx)
        db.finish_run(
            run_id=run_id,
            status="completed",
            exit_code=0,
            error_text=None,
        )

    payload = client.get("/api/tasks/maintenance.quick").json()
    assert payload["task"]["task_id"] == "maintenance.quick"
    assert len(payload["runs"]) == 20


def test_task_detail_endpoint_accepts_human_slug(test_client) -> None:
    client, _main_app = test_client

    payload = client.get("/api/tasks/quick").json()
    assert payload["task"]["task_id"] == "maintenance.quick"
    assert payload["task"]["slug"] == "quick"



def test_library_endpoint_returns_dataset_stats(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_library_dataset_stats",
        lambda: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "stats": {
                "total_documents": 100,
                "metadata_rows": 80,
                "applicable_docs": 25,
                "non_applicable_docs": 10,
                "pending_evaluation": 45,
                "classified_docs": 20,
                "evaluated_docs": 35,
                "acceptance_rate": 71.43,
                "classification_coverage": 80.0,
            },
            "top_classifications": [
                {"ddc": "891.7", "path": "Language / Tatar", "usage_count": 7},
            ],
            "preview_stats": {
                "eligible": 19,
                "ready": 7,
                "pending": 10,
                "partial": 1,
                "failed": 1,
                "generated_preview_pages": 18,
                "generated_image_objects": 36,
            },
        },
    )

    response = client.get("/api/library")
    assert response.status_code == 200
    payload = response.json()
    assert payload["dataset"]["available"] is True
    assert payload["dataset"]["stats"]["applicable_docs"] == 25
    assert payload["dataset"]["top_classifications"][0]["ddc"] == "891.7"
    assert payload["dataset"]["preview_stats"]["ready"] == 7


def test_library_preview_endpoint_returns_variable_manifest(test_client, monkeypatch) -> None:
    client, _main_app = test_client
    md5 = "abcdef0123456789abcdef0123456789"

    class _Repository:
        def __init__(self, _database_url, *, schema):
            _ = schema

        def is_eligible_pdf(self, requested_md5, **_kwargs):
            return requested_md5 == md5

        def get(self, requested_md5):
            assert requested_md5 == md5
            return {
                "md5": md5,
                "status": "ready",
                "recipe_version": "webp-v2",
                "source_page_count": 2,
                "first_preview_page": 1,
                "second_preview_page": None,
                "last_preview_page": 2,
            }

        def dispose(self):
            return None

    monkeypatch.setattr("app.library_preview_routes.LibraryPreviewRepository", _Repository)
    monkeypatch.setattr(
        "app.library_preview_routes.get_book_preview_storage",
        lambda: ("https://s3.test", "documents", "ttpreviews"),
    )

    response = client.get(f"/api/library/previews/{md5}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["expected_preview_count"] == 2
    assert [item["role"] for item in payload["previews"]] == ["first", "last"]
    assert payload["previews"][1]["variants"]["small"]["url"].endswith(
        f"/ttpreviews/{md5}/ls.webp"
    )


def test_library_preview_endpoint_rejects_non_applicable_document(test_client, monkeypatch) -> None:
    client, _main_app = test_client

    class _Repository:
        def __init__(self, _database_url, *, schema):
            _ = schema

        def is_eligible_pdf(self, _md5, **_kwargs):
            return False

        def dispose(self):
            return None

    monkeypatch.setattr("app.library_preview_routes.LibraryPreviewRepository", _Repository)
    monkeypatch.setattr(
        "app.library_preview_routes.get_book_preview_storage",
        lambda: ("https://s3.test", "documents", "ttpreviews"),
    )

    response = client.get("/api/library/previews/abcdef0123456789abcdef0123456789")

    assert response.status_code == 404


def test_database_state_endpoint_returns_snapshot_shape(test_client) -> None:
    client, _main_app = test_client

    response = client.get("/api/database/state")
    assert response.status_code == 200
    payload = response.json()
    assert "database_state" in payload
    snapshot = payload["database_state"]
    assert "available" in snapshot
    assert "backup" in snapshot
    assert "full" in snapshot["backup"]
    assert "incremental" in snapshot["backup"]
