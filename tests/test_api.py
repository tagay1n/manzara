"""API/runtime behavior tests for Manzara."""

from __future__ import annotations

import asyncio
import json
import re
import time

import app.tasks as task_runtime
import pytest
from app.gemini_config import GeminiKey
from app.gemini_runtime import GeminiRequestRejectedError, GeminiRuntimeManager
from app.modules.maintenance.workflow import (
    LIBRARY_WORKFLOW_ID,
    MAINTENANCE_BACKUP_FULL_WORKFLOW_ID,
    MAINTENANCE_BACKUP_INCR_SCHEDULE_ID,
    MAINTENANCE_BACKUP_INCR_WORKFLOW_ID,
    maintenance_backup_incr_workflow_bundle,
)
from app.modules.shayan.workflow import SHAYAN_WEEKLY_SCHEDULE_ID, SHAYAN_WEEKLY_WORKFLOW_ID


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


def test_html_pages_disable_browser_caching(test_client) -> None:
    client, _main_app = test_client

    for path in ("/tasks", "/flows/shayan", "/tasks/shayan.scan_changes"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


def test_static_ui_assets_require_browser_revalidation(test_client) -> None:
    client, _main_app = test_client

    for path in ("/static/styles.css", "/static/shell.js", "/static/shell-state.js"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"


def test_system_state_returns_lightweight_global_payload(test_client) -> None:
    client, _main_app = test_client

    response = client.get("/api/system/state")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload.get("generated_at"), str)
    assert isinstance(payload.get("event_cursor"), int)
    assert set(payload["global"]) >= {
        "active_tasks",
        "active_workflows",
        "stop_all_state",
    }


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
    assert "maintenance.monocorpus_sync" not in maintenance_task_ids
    assert "maintenance.pgbackrest_backup_full" in maintenance_task_ids
    assert "maintenance.pgbackrest_backup_incr" in maintenance_task_ids
    assert "maintenance.monocorpus_meta_evaluate" not in maintenance_task_ids

    library = panels["library"]
    library_task_ids = {task["task_id"] for task in library["tasks"]}
    assert "maintenance.monocorpus_meta_evaluate" in library_task_ids
    assert "library.collection_detect" in library_task_ids
    assert "library.collection_validate" in library_task_ids
    assert "library.collection_apply" in library_task_ids


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
    assert MAINTENANCE_BACKUP_FULL_WORKFLOW_ID in workflow_ids
    assert MAINTENANCE_BACKUP_INCR_WORKFLOW_ID in workflow_ids


def test_update_interval_schedule_minutes(test_client) -> None:
    client, _main_app = test_client

    response = client.patch(
        f"/api/schedules/{MAINTENANCE_BACKUP_INCR_SCHEDULE_ID}",
        json={"schedule_type": "interval", "interval_minutes": 180, "enabled": True},
    )
    assert response.status_code == 200
    schedule = response.json()["schedule"]
    assert schedule["schedule_type"] == "interval"
    assert int(schedule["interval_minutes"]) == 180
    assert schedule["enabled"] is True
    assert schedule["next_run_at"] is not None


def test_incremental_backup_schedule_defaults_to_twelve_hours() -> None:
    bundle = maintenance_backup_incr_workflow_bundle()

    assert bundle["workflow"]["title"] == "Postgres incremental backup (every 12h)"
    assert bundle["schedule"]["interval_minutes"] == 720


def test_update_schedule_rejects_invalid_enabled_string(test_client) -> None:
    client, _main_app = test_client

    response = client.patch(
        f"/api/schedules/{SHAYAN_WEEKLY_SCHEDULE_ID}",
        json={"enabled": "not-a-bool"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "enabled must be a boolean-like value"


def test_update_schedule_rejects_non_integral_day_of_week(test_client) -> None:
    client, _main_app = test_client

    response = client.patch(
        f"/api/schedules/{SHAYAN_WEEKLY_SCHEDULE_ID}",
        json={"day_of_week": 4.5},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "day_of_week must be an integer 1..7"


def test_update_schedule_rejects_non_integral_interval_minutes(test_client) -> None:
    client, _main_app = test_client

    response = client.patch(
        f"/api/schedules/{MAINTENANCE_BACKUP_INCR_SCHEDULE_ID}",
        json={"interval_minutes": 90.5},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "interval_minutes must be an integer >= 1"


def test_update_schedule_accepts_numeric_enabled_zero_and_one(test_client) -> None:
    client, _main_app = test_client

    disable_resp = client.patch(
        f"/api/schedules/{SHAYAN_WEEKLY_SCHEDULE_ID}",
        json={"enabled": 0},
    )
    assert disable_resp.status_code == 200
    assert disable_resp.json()["schedule"]["enabled"] is False

    enable_resp = client.patch(
        f"/api/schedules/{SHAYAN_WEEKLY_SCHEDULE_ID}",
        json={"enabled": 1},
    )
    assert enable_resp.status_code == 200
    assert enable_resp.json()["schedule"]["enabled"] is True


def test_update_schedule_rejects_numeric_enabled_outside_zero_one(test_client) -> None:
    client, _main_app = test_client

    response = client.patch(
        f"/api/schedules/{SHAYAN_WEEKLY_SCHEDULE_ID}",
        json={"enabled": 2},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "enabled must be a boolean-like value"


def test_update_schedule_accepts_valid_iana_timezone(test_client) -> None:
    client, _main_app = test_client

    response = client.patch(
        f"/api/schedules/{SHAYAN_WEEKLY_SCHEDULE_ID}",
        json={"timezone": "Europe/Moscow"},
    )
    assert response.status_code == 200
    assert response.json()["schedule"]["timezone"] == "Europe/Moscow"


def test_update_schedule_rejects_invalid_timezone(test_client) -> None:
    client, _main_app = test_client

    response = client.patch(
        f"/api/schedules/{SHAYAN_WEEKLY_SCHEDULE_ID}",
        json={"timezone": "Invalid/Timezone"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "timezone must be a valid IANA timezone name"


def test_tasks_endpoint_groups_tasks_by_flow(test_client) -> None:
    client, main_app = test_client

    event = main_app.state.db.insert_event(
        "task.started",
        task_id="shayan.quick",
        run_id=999,
        panel_id="shayan",
        payload={"status": "starting"},
    )

    response = client.get("/api/tasks")
    assert response.status_code == 200
    payload = response.json()
    assert payload["event_cursor"] == event["event_id"]
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


def test_task_detail_default_limit_is_twenty(test_client) -> None:
    client, main_app = test_client
    db = main_app.state.db
    task = db.get_task("shayan.quick")
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

    payload = client.get("/api/tasks/shayan.quick").json()
    assert payload["task"]["task_id"] == "shayan.quick"
    assert len(payload["runs"]) == 20


def test_task_detail_endpoint_accepts_human_slug(test_client) -> None:
    client, _main_app = test_client

    payload = client.get("/api/tasks/quick").json()
    assert payload["task"]["task_id"] == "shayan.quick"
    assert payload["task"]["slug"] == "quick"


def test_flow_detail_endpoint_returns_tasks_with_recent_runs_and_summary(
    test_client,
) -> None:
    client, main_app = test_client
    db = main_app.state.db
    task = db.get_task("shayan.quick")
    assert task is not None
    run_id = db.create_run(task)
    db.mark_run_started(run_id, pid=21001)
    db.finish_run(
        run_id=run_id,
        status="completed",
        exit_code=0,
        error_text=None,
    )

    flow_payload = client.get("/api/flows/shayan?limit_per_task=20")
    assert flow_payload.status_code == 200
    payload = flow_payload.json()
    assert payload["flow"]["panel_id"] == "shayan"
    assert payload["flow"]["slug"] == "shayan"
    assert "stats_cards" in payload["flow"] or "stats" in payload["flow"]
    assert len(payload["tasks"]) >= 1

    quick = next(item for item in payload["tasks"] if item["task_id"] == "shayan.quick")
    assert len(quick["runs"]) >= 1
    assert quick["runs"][0]["run_id"] == run_id
    assert isinstance(quick["runs"][0].get("summary"), dict)
    assert quick["runs"][0]["summary"]["status"] == "completed"


def test_flow_detail_endpoint_accepts_flow_slug(test_client) -> None:
    client, _main_app = test_client

    rename = client.patch("/api/flows/shayan/title", json={"title": "Shayan Console"})
    assert rename.status_code == 200

    payload = client.get("/api/flows/shayan-console").json()
    assert payload["flow"]["panel_id"] == "shayan"
    assert payload["flow"]["slug"] == "shayan-console"


def test_shayan_catalog_endpoint_returns_programs_and_episode_flags(test_client) -> None:
    client, main_app = test_client
    db = main_app.state.db

    entry_1 = {
        "category": "cartoons",
        "program": "Show One",
        "season": 1,
        "episode": 1,
        "title": "Pilot",
        "file": "videos/cartoons/Show One/S01/S01E01.mkv",
    }
    entry_2 = {
        "category": "shows",
        "program": "Show Two",
        "season": 2,
        "episode": 3,
        "title": "Episode 3",
        "file": "videos/shows/Show Two/S02/S02E03.mkv",
    }
    db.create_shayan_snapshot({"ep-1": entry_1, "ep-2": entry_2})
    db.replace_shayan_manifest_entries({"ep-1": entry_1})
    with db._connect() as conn:
        conn.execute(
            """
            UPDATE shayan_manifest_entries
            SET
                yadisk_status = 'uploaded',
                yadisk_uploaded_payload_hash = payload_hash,
                yadisk_remote_path = '/remote/videos/cartoons/Show One/S01/S01E01.mkv'
            WHERE entry_key = ?
            """,
            ("ep-1",),
        )

    local_path = main_app.state.settings.shayan.output_path / "videos" / "cartoons" / "Show One" / "S01"
    local_path.mkdir(parents=True, exist_ok=True)
    (local_path / "S01E01.mkv").write_text("video-bytes", encoding="utf-8")

    response = client.get("/api/shayan/catalog")
    assert response.status_code == 200
    payload = response.json()
    assert int(payload["stats"]["programs"]) == 2
    assert int(payload["stats"]["episodes"]) == 2
    assert int(payload["stats"]["downloaded"]) == 1
    assert int(payload["stats"]["uploaded"]) == 1

    programs = payload["programs"]
    show_one = next(item for item in programs if item["program"] == "Show One")
    show_one_ep = show_one["episodes"][0]
    assert show_one_ep["entry_key"] == "ep-1"
    assert show_one_ep["downloaded"] is True
    assert show_one_ep["uploaded"] is True
    assert show_one_ep["season"] == 1
    assert show_one_ep["episode"] == 1

    show_two = next(item for item in programs if item["program"] == "Show Two")
    show_two_ep = show_two["episodes"][0]
    assert show_two_ep["entry_key"] == "ep-2"
    assert show_two_ep["downloaded"] is False
    assert show_two_ep["uploaded"] is False


def test_shayan_redownload_episode_resets_manifest_and_requests_download(test_client) -> None:
    client, main_app = test_client
    db = main_app.state.db

    entry = {
        "category": "cartoons",
        "program": "Show One",
        "season": 1,
        "episode": 1,
        "title": "Pilot",
        "file": "videos/cartoons/Show One/S01/S01E01.mkv",
    }
    db.create_shayan_snapshot({"ep-1": entry})
    db.replace_shayan_manifest_entries({"ep-1": entry})
    with db._connect() as conn:
        conn.execute(
            """
            UPDATE shayan_manifest_entries
            SET
                yadisk_status = 'uploaded',
                yadisk_uploaded_payload_hash = payload_hash,
                yadisk_remote_path = '/remote/videos/cartoons/Show One/S01/S01E01.mkv'
            WHERE entry_key = ?
            """,
            ("ep-1",),
        )

    local_dir = main_app.state.settings.shayan.output_path / "videos" / "cartoons" / "Show One" / "S01"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_file = local_dir / "S01E01.mkv"
    local_file.write_text("video-bytes", encoding="utf-8")

    captured: dict[str, str] = {}

    def _fake_start_task(task_id, *, sudo_password=None):
        captured["task_id"] = str(task_id)
        _ = sudo_password
        return {"action": "captured", "run": None}

    main_app.state.runner.start_task = _fake_start_task  # type: ignore[method-assign]

    response = client.post("/api/shayan/episodes/ep-1/redownload")
    assert response.status_code == 200
    payload = response.json()
    assert payload["entry_key"] == "ep-1"
    assert payload["manifest_deleted"] is True
    assert payload["local_deleted"] is True
    assert payload["download"]["action"] == "captured"
    assert captured["task_id"] == "shayan.download_new"

    assert local_file.exists() is False
    assert "ep-1" not in db.list_shayan_manifest_entries()

    catalog = client.get("/api/shayan/catalog").json()
    show_one = next(item for item in catalog["programs"] if item["program"] == "Show One")
    episode = show_one["episodes"][0]
    assert episode["entry_key"] == "ep-1"
    assert episode["downloaded"] is False
    assert episode["uploaded"] is False


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

        def is_eligible_pdf(self, requested_md5):
            return requested_md5 == md5

        def get(self, requested_md5):
            assert requested_md5 == md5
            return {
                "md5": md5,
                "status": "ready",
                "recipe_version": "pdf-three-page-webp-v1",
                "source_page_count": 2,
                "manifest": {
                    "first": {
                        "page_number": 1,
                        "variants": {"small": {"key": "prefix/1s.webp"}},
                    },
                    "last": {
                        "page_number": 2,
                        "variants": {"small": {"key": "prefix/ls.webp"}},
                    },
                },
            }

        def dispose(self):
            return None

    monkeypatch.setattr("app.library_preview_routes.LibraryPreviewRepository", _Repository)
    monkeypatch.setattr(
        "app.library_preview_routes.get_book_preview_bucket",
        lambda: "ttbook-previews",
    )

    response = client.get(f"/api/library/previews/{md5}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["expected_preview_count"] == 2
    assert [item["role"] for item in payload["previews"]] == ["first", "last"]
    assert payload["previews"][1]["variants"]["small"]["url"].endswith("/prefix/ls.webp")


def test_library_preview_endpoint_rejects_non_applicable_document(test_client, monkeypatch) -> None:
    client, _main_app = test_client

    class _Repository:
        def __init__(self, _database_url, *, schema):
            _ = schema

        def is_eligible_pdf(self, _md5):
            return False

        def dispose(self):
            return None

    monkeypatch.setattr("app.library_preview_routes.LibraryPreviewRepository", _Repository)

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


def test_gemini_state_endpoint_returns_grouped_keys(test_client, monkeypatch) -> None:
    client, _main_app = test_client

    monkeypatch.setattr(
        "app.gemini_runtime.load_gemini_keys",
        lambda: [
            GeminiKey(
                account_id="acc-a",
                key_id="acc-a:key-1",
                key_value="KEY_A_1",
                masked_key="KEYA...A001",
            ),
            GeminiKey(
                account_id="acc-b",
                key_id="acc-b:key-2",
                key_value="KEY_B_2",
                masked_key="KEYB...B002",
            ),
        ],
    )

    response = client.get("/api/gemini/state")
    assert response.status_code == 200
    response_payload = response.json()
    assert isinstance(response_payload["event_cursor"], int)
    payload = response_payload["gemini"]
    assert payload["summary"]["accounts"] == 2
    assert payload["summary"]["keys"] == 2
    assert {item["account_id"] for item in payload["accounts"]} == {"acc-a", "acc-b"}
    assert "global" in payload


def test_gemini_reset_key_and_reset_all_clear_exhaustion(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        "app.gemini_runtime.load_gemini_keys",
        lambda: [
            GeminiKey(
                account_id="acc-a",
                key_id="acc-a:key-1",
                key_value="KEY_A_1",
                masked_key="KEYA...A001",
            ),
            GeminiKey(
                account_id="acc-a",
                key_id="acc-a:key-2",
                key_value="KEY_A_2",
                masked_key="KEYA...A002",
            ),
        ],
    )

    _ = client.get("/api/gemini/state")
    db = main_app.state.db
    db.ensure_gemini_model_state("acc-a:key-1", "gemini-2.5-flash")
    db.ensure_gemini_model_state("acc-a:key-2", "gemini-2.5-flash")
    now_ts = "2026-03-25T00:00:00+00:00"
    db.mark_gemini_error(
        "acc-a:key-1",
        "gemini-2.5-flash",
        now_ts=now_ts,
        error_text="quota",
        exhausted=True,
    )
    db.mark_gemini_error(
        "acc-a:key-2",
        "gemini-2.5-flash",
        now_ts=now_ts,
        error_text="quota",
        exhausted=True,
    )

    one = client.post("/api/gemini/reset-key", json={"key_id": "acc-a:key-1"})
    assert one.status_code == 200
    assert one.json()["rows_changed"] >= 1

    rows_after_one = db.list_gemini_model_states(model_name="gemini-2.5-flash")
    by_key = {str(item["key_id"]): bool(item.get("exhausted")) for item in rows_after_one if item.get("model_name")}
    assert by_key["acc-a:key-1"] is False
    assert by_key["acc-a:key-2"] is True

    all_resp = client.post("/api/gemini/reset-all")
    assert all_resp.status_code == 200
    assert all_resp.json()["rows_changed"] >= 1

    rows_after_all = db.list_gemini_model_states(model_name="gemini-2.5-flash")
    assert all(bool(item.get("exhausted")) is False for item in rows_after_all if item.get("model_name"))


def test_gemini_reset_key_rejects_missing_or_blank_key_id(test_client) -> None:
    client, _main_app = test_client

    missing = client.post("/api/gemini/reset-key", json={})
    assert missing.status_code == 400
    assert missing.json()["detail"] == "key_id is required"

    blank = client.post("/api/gemini/reset-key", json={"key_id": "   "})
    assert blank.status_code == 400
    assert blank.json()["detail"] == "key_id is required"


def test_gemini_400_rejection_does_not_exhaust_or_pause_key(test_client, monkeypatch) -> None:
    _client, main_app = test_client
    monkeypatch.setattr(
        "app.gemini_runtime.load_gemini_keys",
        lambda: [
            GeminiKey(
                account_id="acc-a",
                key_id="acc-a:key-1",
                key_value="KEY_A_1",
                masked_key="KEYA...A001",
            ),
        ],
    )

    manager = GeminiRuntimeManager(
        main_app.state.db,
        task_id="library.personality_suggestions_refresh",
        panel_id="library",
    )

    class _BadRequestError(Exception):
        status_code = 400

        def __str__(self) -> str:
            return "bad prompt payload"

    def _raise_400(_api_key: str, _lease) -> None:
        raise _BadRequestError()

    with pytest.raises(GeminiRequestRejectedError):
        manager.run_with_key(
            model_name="gemini-2.5-flash",
            call=_raise_400,
            max_attempts=2,
        )

    rows = main_app.state.db.list_gemini_model_states(model_name="gemini-2.5-flash")
    assert len(rows) == 1
    row = rows[0]
    assert bool(row.get("exhausted")) is False
    assert row.get("last_error_text") in (None, "")

    control = main_app.state.db.ensure_gemini_runtime_control("2026-03-25")
    assert control.get("pause_until") is None


def test_library_classifications_table_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "list_classifications",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "page": 2,
            "page_size": 10,
            "total": 23,
            "total_pages": 3,
            "items": [
                {
                    "classification_id": 7,
                    "ddc": "891.7",
                    "path": "Language / Tatar",
                    "status": "approved",
                    "created_by": "gemini",
                    "created_at": "2026-03-01T10:00:00",
                    "usage_count": 12,
                }
            ],
        },
    )

    response = client.get("/api/library/classifications?page=2&page_size=10")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["page"] == 2
    assert payload["items"][0]["classification_id"] == 7


def test_library_classification_insights_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_classification_insights",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "tree": [{"name": "Language", "usage_count": 10, "children": []}],
            "distribution": [{"bucket": "800", "usage_count": 10, "share_pct": 100.0}],
            "duplicates": [],
            "unclassified_queue": {"total": 1, "items": [{"md5": "abc"}]},
        },
    )

    response = client.get("/api/library/classifications/insights")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["distribution"][0]["bucket"] == "800"
    assert payload["unclassified_queue"]["total"] == 1


def test_library_classification_normalization_preview_endpoint(
    test_client,
    monkeypatch,
) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_normalization_preview",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "rules": {"drop_segments": ["turkic literature"]},
            "summary": {
                "total_rows_scanned": 100,
                "affected_classifications": 7,
                "estimated_reassigned_documents": 42,
                "merge_group_candidates": 3,
            },
            "affected_preview": [{"classification_id": 1}],
            "merge_groups": [{"normalized_path": "language / tatar"}],
        },
    )

    response = client.get(
        "/api/library/classifications/normalization-preview"
        "?drop_segments=Turkic%20literature,Tatar&limit=120&row_limit=5000"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["summary"]["affected_classifications"] == 7
    assert payload["rules"]["drop_segments"][0] == "turkic literature"


def test_library_classification_merge_candidates_endpoint(
    test_client,
    monkeypatch,
) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_merge_candidates",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "summary": {
                "rows_scanned": 120,
                "candidate_count": 2,
                "min_score": 0.78,
            },
            "candidates": [
                {
                    "issue": "near_duplicate",
                    "score": 0.91,
                    "impact": 11,
                    "recommended_primary_classification_id": 3,
                    "primary": {"classification_id": 3, "ddc": "891.7", "path": "A", "usage_count": 7},
                    "secondary": {"classification_id": 4, "ddc": "891.7", "path": "B", "usage_count": 4},
                }
            ],
        },
    )

    response = client.get(
        "/api/library/classifications/merge-candidates"
        "?limit=80&min_score=0.8&row_limit=1000"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["summary"]["candidate_count"] == 2
    assert payload["candidates"][0]["recommended_primary_classification_id"] == 3


def test_library_classification_merge_endpoint(
    test_client,
    monkeypatch,
) -> None:
    client, main_app = test_client
    captured: dict[str, int | str] = {}

    def _fake_merge_classifications(*, source_classification_id, target_classification_id, reason):  # noqa: ANN001
        captured["source"] = int(source_classification_id)
        captured["target"] = int(target_classification_id)
        captured["reason"] = str(reason)
        return {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "source_classification_id": int(source_classification_id),
            "target_classification_id": int(target_classification_id),
            "moved_docs_count": 17,
            "schema_org_updated_count": 17,
            "source_deleted": True,
        }

    monkeypatch.setattr(main_app, "merge_classifications", _fake_merge_classifications)

    response = client.post(
        "/api/library/classifications/merge",
        json={
            "source_classification_id": 7,
            "target_classification_id": 3,
            "reason": "manual_merge",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["source_classification_id"] == 7
    assert payload["target_classification_id"] == 3
    assert payload["moved_docs_count"] == 17
    assert payload["source_deleted"] is True
    assert captured == {"source": 7, "target": 3, "reason": "manual_merge"}


def test_library_classification_merge_endpoint_rejects_invalid_ids(
    test_client,
) -> None:
    client, _main_app = test_client
    response = client.post(
        "/api/library/classifications/merge",
        json={
            "source_classification_id": "x",
            "target_classification_id": 3,
        },
    )
    assert response.status_code == 400
    assert "must be integers" in response.json().get("detail", "")


def test_library_personalities_overview_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_personality_overview",
        lambda: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "stats": {
                "total_mentions": 120,
                "docs_with_authors": 80,
                "unique_raw_names": 40,
                "unique_normalized_names": 30,
                "mixed_script_mentions": 5,
                "patronymic_mentions": 12,
            },
            "top_personalities": [{"raw_name": "Габдулла Тукай", "docs_count": 9}],
        },
    )

    response = client.get("/api/library/personalities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["available"] is True
    assert payload["overview"]["stats"]["total_mentions"] == 120
    assert payload["overview"]["top_personalities"][0]["raw_name"] == "Габдулла Тукай"


def test_library_personalities_table_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "list_personalities",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "page": 1,
            "page_size": 25,
            "total": 1,
            "total_pages": 1,
            "items": [
                {
                    "raw_name": "Габдулла Тукай",
                    "normalized_name": "габдулла тукай",
                    "script_label": "cyrillic",
                    "docs_count": 9,
                    "mentions_count": 10,
                    "patronymic_mentions": 0,
                }
            ],
        },
    )

    response = client.get("/api/library/personalities/table?page=1&page_size=25")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["items"][0]["raw_name"] == "Габдулла Тукай"


def test_library_personalities_insights_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_personality_insights",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "script_distribution": [{"script_label": "cyrillic", "mentions_count": 10, "share_pct": 100.0}],
            "variant_clusters": [{"normalized_name": "габдулла тукай", "variants_count": 2}],
            "ambiguous_queue": {"total": 1, "items": [{"raw_name": "Тукай"}]},
        },
    )

    response = client.get("/api/library/personalities/insights")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["script_distribution"][0]["script_label"] == "cyrillic"
    assert payload["ambiguous_queue"]["total"] == 1


def test_library_publishers_overview_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_publisher_overview",
        lambda: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "stats": {
                "total_mentions": 90,
                "docs_with_publishers": 70,
                "unique_raw_names": 35,
                "unique_normalized_names": 28,
                "mixed_script_mentions": 4,
                "org_marker_mentions": 17,
            },
            "top_publishers": [{"raw_name": "Таткнигоиздат", "docs_count": 11}],
        },
    )

    response = client.get("/api/library/publishers")
    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["available"] is True
    assert payload["overview"]["stats"]["docs_with_publishers"] == 70
    assert payload["overview"]["top_publishers"][0]["raw_name"] == "Таткнигоиздат"


def test_library_publishers_table_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "list_publishers",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "page": 1,
            "page_size": 25,
            "total": 1,
            "total_pages": 1,
            "items": [
                {
                    "raw_name": "Таткнигоиздат",
                    "normalized_name": "таткнигоиздат",
                    "script_label": "cyrillic",
                    "docs_count": 11,
                    "mentions_count": 12,
                    "org_marker_mentions": 0,
                }
            ],
        },
    )

    response = client.get("/api/library/publishers/table?page=1&page_size=25")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["items"][0]["raw_name"] == "Таткнигоиздат"


def test_library_publishers_insights_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_publisher_insights",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "script_distribution": [{"script_label": "cyrillic", "mentions_count": 15, "share_pct": 100.0}],
            "variant_clusters": [{"normalized_name": "таткнигоиздат", "variants_count": 2}],
            "ambiguous_queue": {"total": 1, "items": [{"raw_name": "Татиздат"}]},
        },
    )

    response = client.get("/api/library/publishers/insights")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["script_distribution"][0]["script_label"] == "cyrillic"
    assert payload["ambiguous_queue"]["total"] == 1


def test_library_collections_overview_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_collection_overview",
        lambda: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "stats": {
                "total_collections": 12,
                "approved_collections": 5,
                "included_collections": 4,
                "suggested_collections": 6,
                "items_linked": 190,
            },
            "top_collections": [
                {
                    "collection_id": 101,
                    "title": "Шура журналы",
                    "item_count": 40,
                    "status": "approved",
                    "include_in_library": True,
                }
            ],
        },
    )

    response = client.get("/api/library/collections")
    assert response.status_code == 200
    payload = response.json()
    assert payload["overview"]["available"] is True
    assert payload["overview"]["stats"]["total_collections"] == 12
    assert payload["overview"]["top_collections"][0]["collection_id"] == 101


def test_library_collections_table_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "list_library_collections",
        lambda **_kwargs: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "page": 2,
            "page_size": 20,
            "total": 31,
            "total_pages": 2,
            "items": [
                {
                    "collection_id": 7,
                    "title": "Казан утлары",
                    "normalized_title": "казан утлары",
                    "status": "suggested",
                    "include_in_library": True,
                    "confidence": 0.88,
                    "item_count": 24,
                    "last_detected_at": "2026-03-25T10:00:00+00:00",
                }
            ],
        },
    )

    response = client.get("/api/library/collections/table?page=2&page_size=20")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["page"] == 2
    assert payload["items"][0]["collection_id"] == 7


def test_library_collection_items_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "list_collection_items",
        lambda collection_id, **_kwargs: {
            "available": True,
            "error": None,
            "collection_id": collection_id,
            "items": [
                {
                    "md5": "abc123",
                    "item_title": "Казан утлары №1 (1999)",
                    "ya_path": "/library/kazan-utlary/1999-01.pdf",
                    "lib": False,
                }
            ],
        },
    )

    response = client.get("/api/library/collections/9/items")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["collection_id"] == 9
    assert payload["items"][0]["md5"] == "abc123"


def test_library_collection_review_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_collection_review",
        lambda collection_id, **_kwargs: {
            "available": True,
            "error": None,
            "collection_id": collection_id,
            "summary": {"item_count": 24, "outliers": 2},
            "samples": [],
            "outliers": [],
        },
    )

    response = client.get("/api/library/collections/9/review")

    assert response.status_code == 200
    payload = response.json()
    assert payload["collection_id"] == 9
    assert payload["summary"]["item_count"] == 24


def test_library_collection_proposals_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client
    monkeypatch.setattr(
        main_app,
        "list_collection_proposals",
        lambda **kwargs: {
            "available": True,
            "page": kwargs["page"],
            "total_pages": 1,
            "total": 1,
            "items": [{"proposal_id": 17, "status": kwargs["status"]}],
        },
    )

    response = client.get("/api/library/collection-proposals?status=review_ready&page=1")

    assert response.status_code == 200
    assert response.json()["items"][0]["proposal_id"] == 17


def test_library_collection_proposal_decision_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client
    monkeypatch.setattr(
        main_app,
        "decide_collection_proposal",
        lambda _db, proposal_id, decision, selected_md5s: {
            "ok": True,
            "proposal_id": proposal_id,
            "decision": decision,
            "selected_count": len(selected_md5s),
        },
    )

    response = client.post(
        "/api/library/collection-proposals/17/decision",
        json={"decision": "approve", "selected_md5s": ["a" * 32, "b" * 32]},
    )

    assert response.status_code == 200
    assert response.json()["selected_count"] == 2


def test_library_collection_update_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "update_collection",
        lambda _db, collection_id, updates: {
            "ok": True,
            "collection": {
                "collection_id": collection_id,
                "status": updates.get("status", "suggested"),
                "include_in_library": bool(updates.get("include_in_library", False)),
                "title": str(updates.get("title") or "Collection"),
                "notes": str(updates.get("notes") or ""),
            },
            "updated_fields": sorted(list(updates.keys())),
        },
    )

    response = client.patch(
        "/api/library/collections/12",
        json={
            "status": "approved",
            "include_in_library": True,
            "title": "Шура журналы",
            "notes": "Manual review approved",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["collection"]["collection_id"] == 12
    assert payload["collection"]["status"] == "approved"
    assert payload["collection"]["include_in_library"] is True


def test_library_collection_merge_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "merge_collections",
        lambda _db, source_collection_id, target_collection_id: {
            "ok": True,
            "source_collection_id": source_collection_id,
            "target_collection_id": target_collection_id,
            "moved_items": 6,
        },
    )

    response = client.post(
        "/api/library/collections/41/merge",
        json={"target_collection_id": 38},
    )

    assert response.status_code == 200
    assert response.json()["source_collection_id"] == 41
    assert response.json()["target_collection_id"] == 38


def test_library_document_open_redirects_to_resolved_storage(test_client, monkeypatch) -> None:
    client, _main_app = test_client
    from app import library_document_routes

    monkeypatch.setattr(
        library_document_routes,
        "resolve_document_open_url",
        lambda _state, md5: f"https://objects.example.test/{md5}.pdf",
    )

    digest = "a" * 32
    response = client.get(
        f"/api/library/documents/{digest}/open",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == f"https://objects.example.test/{digest}.pdf"


def test_library_normalization_overview_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_normalization_dashboard",
        lambda _db, _entity_type: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "stats": {
                "total_aliases": 30,
                "docs_with_entities": 20,
                "canonicals": 9,
                "linked": 7,
                "unreviewed": 12,
                "suggested": 6,
                "coverage_pct": 40.0,
            },
            "suggestions": {"open_total": 6, "high": 2, "medium": 3, "low": 1},
            "top_unresolved": [{"raw_name": "Тукай", "docs_count": 5}],
        },
    )
    monkeypatch.setattr(
        main_app,
        "get_normalization_quality",
        lambda _db, _entity_type: {"available": True, "error": None, "stats": {"coverage_pct": 40.0}},
    )
    monkeypatch.setattr(
        main_app,
        "list_suggestions",
        lambda _db, _entity_type, limit=80: {"available": True, "error": None, "items": [{"raw_name": "Тукай"}][:limit]},
    )
    monkeypatch.setattr(
        main_app,
        "list_normalization_history",
        lambda _db, _entity_type, limit=20: {"available": True, "error": None, "items": [{"event_id": 1}][:limit]},
    )

    response = client.get("/api/library/normalization/personality")
    assert response.status_code == 200
    payload = response.json()
    assert payload["entity_type"] == "personality"
    assert payload["dashboard"]["available"] is True
    assert payload["dashboard"]["stats"]["total_aliases"] == 30
    assert payload["quality"]["available"] is True
    assert payload["suggestions"]["items"][0]["raw_name"] == "Тукай"
    assert payload["history_preview"]["items"][0]["event_id"] == 1


def test_library_normalization_queue_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_review_queue",
        lambda _db, _entity_type, **_kwargs: {
            "available": True,
            "error": None,
            "page": 1,
            "page_size": 40,
            "total": 1,
            "total_pages": 1,
            "items": [
                {
                    "raw_name": "Тукай",
                    "normalized_name": "тукай",
                    "script_label": "cyrillic",
                    "docs_count": 4,
                    "mentions_count": 5,
                    "queue_status": "unreviewed",
                }
            ],
        },
    )

    response = client.get("/api/library/normalization/personality/queue?status=all&page=1&page_size=40")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["items"][0]["raw_name"] == "Тукай"
    assert payload["items"][0]["queue_status"] == "unreviewed"


def test_library_normalization_link_decision_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "link_alias",
        lambda _db, _entity_type, **kwargs: {
            "alias": {
                "raw_name": kwargs["raw_name"],
                "canonical_id": kwargs["canonical_id"],
                "decision_status": "linked",
            },
            "event": {"event_id": 4},
        },
    )

    response = client.post(
        "/api/library/normalization/personality/decisions/link",
        json={"raw_name": "Тукай", "canonical_id": 9, "suggestion_ids": [3]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["alias"]["raw_name"] == "Тукай"
    assert payload["alias"]["canonical_id"] == 9
    assert payload["event"]["event_id"] == 4


def test_library_normalization_link_rejects_invalid_suggestion_ids(test_client) -> None:
    client, _main_app = test_client

    response = client.post(
        "/api/library/normalization/personality/decisions/link",
        json={"raw_name": "Тукай", "canonical_id": 9, "suggestion_ids": ["bad"]},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "suggestion_ids must be integers"


def test_library_normalization_reject_rejects_invalid_suggestion_ids(test_client) -> None:
    client, _main_app = test_client

    response = client.post(
        "/api/library/normalization/personality/decisions/reject",
        json={"raw_name": "Тукай", "suggestion_ids": ["bad"]},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "suggestion_ids must be integers"


def test_library_normalization_bulk_link_rejects_invalid_raw_names_shape(test_client) -> None:
    client, _main_app = test_client

    response = client.post(
        "/api/library/normalization/personality/bulk/link",
        json={"raw_names": "Alias One", "canonical_id": 9},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "raw_names must be a list of strings"


def test_library_normalization_bulk_reject_rejects_invalid_raw_names_shape(test_client) -> None:
    client, _main_app = test_client

    response = client.post(
        "/api/library/normalization/personality/bulk/reject",
        json={"raw_names": "Alias One"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "raw_names must be a list of strings"


def test_library_normalization_refresh_suggestions_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "refresh_suggestions",
        lambda _db, _entity_type, limit, use_gemini: {
            "generated": limit,
            "bands": {"high": 1, "medium": 2, "low": 3},
            "event": {"event_id": 9, "use_gemini": use_gemini},
        },
    )

    response = client.post(
        "/api/library/normalization/publisher/suggestions/refresh",
        json={"limit": 77, "use_gemini": False},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["generated"] == 77
    assert payload["event"]["event_id"] == 9
    assert payload["event"]["use_gemini"] is False


def test_library_normalization_rejects_unknown_entity(test_client) -> None:
    client, _main_app = test_client

    response = client.get("/api/library/normalization/unknown")
    assert response.status_code == 404


def test_library_classification_detail_endpoint(test_client, monkeypatch) -> None:
    client, main_app = test_client

    monkeypatch.setattr(
        main_app,
        "get_classification_detail",
        lambda classification_id, docs_page, docs_page_size: {
            "available": True,
            "error": None,
            "config_source": "config.yaml",
            "classification": {
                "classification_id": classification_id,
                "ddc": "891.7",
                "path": "Language / Tatar",
                "path_tt": "Тел / Татар",
                "status": "approved",
                "created_by": "gemini",
                "created_at": "2026-03-01T10:00:00",
                "usage_count": 9,
            },
            "linked_docs": {
                "page": docs_page,
                "page_size": docs_page_size,
                "total": 1,
                "total_pages": 1,
                "items": [{"md5": "abc"}],
            },
            "language_distribution": [{"language": "tt-Cyrl", "count": 1}],
        },
    )

    response = client.get("/api/library/classifications/7?docs_page=1&docs_page_size=20")
    assert response.status_code == 200
    payload = response.json()
    assert payload["detail"]["available"] is True
    assert payload["detail"]["classification"]["classification_id"] == 7
    assert payload["detail"]["linked_docs"]["items"][0]["md5"] == "abc"


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


def test_toggle_task_reports_sudo_password_required(test_client, monkeypatch) -> None:
    client, main_app = test_client

    def _always_require(_task, *, sudo_password=None):
        _ = sudo_password
        return {
            "ok": False,
            "reason": "sudo_password_required",
            "message": "Sudo password is required for this command.",
        }

    monkeypatch.setattr(main_app.state.runner, "_check_sudo_requirements", _always_require)
    response = client.post("/api/tasks/maintenance.pgbackrest_backup_full/toggle")
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "sudo_password_required"
    assert payload["reason"] == "sudo_password_required"


def test_sudo_preflight_checks_exact_command_policy(test_client, monkeypatch) -> None:
    client, _main_app = test_client
    captured = {}

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "sudo: a password is required"

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = dict(kwargs)
        return _Result()

    monkeypatch.setattr(task_runtime.subprocess, "run", _fake_run)

    response = client.post("/api/tasks/maintenance.pgbackrest_backup_incr/toggle")
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "sudo_password_required"

    probe_cmd = captured["cmd"]
    assert "-l" in probe_cmd
    assert "--" in probe_cmd
    assert any("pgbackrest" in token for token in probe_cmd)


def test_run_workflow_now_passes_sudo_password(test_client, monkeypatch) -> None:
    client, main_app = test_client
    captured = {}

    def _trigger(workflow_id, *, trigger_source, schedule_id=None, sudo_password=None):
        captured["workflow_id"] = workflow_id
        captured["trigger_source"] = trigger_source
        captured["schedule_id"] = schedule_id
        captured["sudo_password"] = sudo_password
        return {"action": "noop", "reason": "captured"}

    monkeypatch.setattr(main_app.state.workflow_service, "trigger_workflow", _trigger)
    response = client.post(
        f"/api/workflows/{MAINTENANCE_BACKUP_FULL_WORKFLOW_ID}/run",
        json={"sudo_password": "secret-pass"},
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "captured"
    assert captured["workflow_id"] == MAINTENANCE_BACKUP_FULL_WORKFLOW_ID
    assert captured["trigger_source"] == "manual"
    assert captured["schedule_id"] is None
    assert captured["sudo_password"] == "secret-pass"


def test_toggle_task_start_and_complete(test_client, wait_for_terminal_run) -> None:
    client, main_app = test_client

    response = client.post("/api/tasks/shayan.quick/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])

    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "completed"

    logs = client.get(f"/api/runs/{run_id}/logs").json()["lines"]
    assert any("quick-ok" in line["line"] for line in logs)


def test_task_artifact_event_and_summary_without_log_parsing(
    test_client,
    wait_for_terminal_run,
) -> None:
    client, main_app = test_client

    main_app.state.db.seed_tasks(
        [
            {
                "task_id": "maintenance.artifact_file_emit",
                "panel_id": "maintenance",
                "title": "artifact file emit",
                "task_type": "test",
                "icon_idle": "Play",
                "icon_running": "Square",
                "cwd": ".",
                "command": {
                    "mode": "shell",
                    "value": (
                        "python3 -c \"import json,os,pathlib; "
                        "p=pathlib.Path(os.environ['MANZARA_RUN_ARTIFACT_PATH']); "
                        "p.parent.mkdir(parents=True,exist_ok=True); "
                        "tmp=p.with_suffix(p.suffix + '.tmp'); "
                        "tmp.write_text(json.dumps({'kind':'test.summary','items_processed':3}),encoding='utf-8'); "
                        "tmp.replace(p); "
                        "print('runtime done')\""
                    ),
                },
            }
        ]
    )

    response = client.post("/api/tasks/maintenance.artifact_file_emit/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "completed"

    artifacts = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        run_payload = main_app.state.db.get_run(run_id)
        summary = run_payload.get("summary") if isinstance(run_payload, dict) else {}
        current = summary.get("artifacts") if isinstance(summary, dict) else None
        if isinstance(current, dict) and current.get("kind"):
            artifacts = current
            break
        time.sleep(0.05)

    assert isinstance(artifacts, dict)
    assert artifacts.get("kind") == "test.summary"
    assert int(artifacts.get("items_processed") or 0) == 3

    events = main_app.state.db.get_events_after(0, limit=400)
    artifact_events = [event for event in events if str(event.get("type") or "") == "task.artifact"]
    assert artifact_events
    latest = artifact_events[-1]
    assert int(latest.get("run_id") or 0) == run_id
    payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
    assert payload.get("kind") == "test.summary"


def test_run_logs_support_tail_and_backfill_pagination(test_client) -> None:
    client, main_app = test_client
    task = main_app.state.db.get_task("shayan.quick")
    assert task is not None
    run_id = main_app.state.db.create_run(task)
    main_app.state.db.mark_run_started(run_id, pid=99999)
    for index in range(1, 21):
        main_app.state.db.append_log(run_id, "stdout", f"line-{index:02d}")
    main_app.state.db.finish_run(run_id, "completed", 0, None)

    all_payload = client.get(f"/api/runs/{run_id}/logs?limit=2000")
    assert all_payload.status_code == 200
    all_lines = all_payload.json()["lines"]
    assert len(all_lines) >= 20
    all_ids = [int(item["log_id"]) for item in all_lines]

    tail_payload = client.get(f"/api/runs/{run_id}/logs?tail=true&limit=5")
    assert tail_payload.status_code == 200
    tail = tail_payload.json()
    tail_ids = [int(item["log_id"]) for item in tail["lines"]]
    assert tail_ids == all_ids[-5:]
    assert int(tail["next_after_log_id"]) == all_ids[-1]
    assert int(tail["next_before_log_id"]) == all_ids[-5]
    assert tail["has_more_before"] is True

    backfill_payload = client.get(
        f"/api/runs/{run_id}/logs?before_log_id={tail['next_before_log_id']}&limit=4"
    )
    assert backfill_payload.status_code == 200
    backfill = backfill_payload.json()
    backfill_ids = [int(item["log_id"]) for item in backfill["lines"]]
    assert backfill_ids == all_ids[-9:-5]
    assert int(backfill["next_after_log_id"]) == all_ids[-6]
    assert int(backfill["next_before_log_id"]) == all_ids[-9]
    assert backfill["has_more_before"] is True


def test_run_logs_reject_conflicting_cursor_modes(test_client) -> None:
    client, main_app = test_client
    task = main_app.state.db.get_task("shayan.quick")
    assert task is not None
    run_id = main_app.state.db.create_run(task)

    conflict = client.get(f"/api/runs/{run_id}/logs?tail=true&after_log_id=10")
    assert conflict.status_code == 400
    assert "cannot be combined" in conflict.json()["detail"]


def test_run_shayan_changes_endpoint_returns_paginated_rows(test_client) -> None:
    client, main_app = test_client
    task = main_app.state.db.get_task("shayan.scan_changes")
    assert task is not None
    run_id = main_app.state.db.create_run(task)
    main_app.state.db.mark_run_started(run_id, pid=1234)
    main_app.state.db.finish_run(run_id, "completed", 0, None)

    main_app.state.db.replace_shayan_run_changes(
        run_id,
        [
            {
                "change_type": "added",
                "entry_key": "a",
                "category": "cartoons",
                "program": "Alpha",
                "season": 1,
                "episode": 1,
                "title": "One",
                "old_payload": {},
                "new_payload": {"title": "One"},
            },
            {
                "change_type": "changed",
                "entry_key": "b",
                "category": "cartoons",
                "program": "Beta",
                "season": 1,
                "episode": 2,
                "title": "Two",
                "old_payload": {"title": "Old"},
                "new_payload": {"title": "Two"},
            },
            {
                "change_type": "removed",
                "entry_key": "c",
                "category": "cartoons",
                "program": "Gamma",
                "season": 1,
                "episode": 3,
                "title": "Three",
                "old_payload": {"title": "Three"},
                "new_payload": {},
            },
        ],
    )

    first = client.get(f"/api/runs/{run_id}/shayan-changes?change_type=added&limit=1")
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["stats"]["added"] == 1
    assert first_payload["stats"]["changed"] == 1
    assert first_payload["stats"]["removed"] == 1
    assert len(first_payload["items"]) == 1
    assert first_payload["items"][0]["change_type"] == "added"
    assert first_payload["has_more"] is False

    invalid = client.get(f"/api/runs/{run_id}/shayan-changes?change_type=unknown")
    assert invalid.status_code == 400
    assert "change_type must be one of" in invalid.json()["detail"]

def test_task_run_writes_artifact_log_with_uniform_format(
    test_client,
    wait_for_terminal_run,
    tmp_path,
) -> None:
    client, main_app = test_client
    artifacts_root = tmp_path / "_artifacts" / "task_runs"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    main_app.state.runner._artifacts_root = artifacts_root

    response = client.post("/api/tasks/shayan.quick/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "completed"

    run_log_path = artifacts_root / "shayan.quick" / f"run-{run_id}.log"
    assert run_log_path.exists()

    lines = run_log_path.read_text(encoding="utf-8").splitlines()
    assert lines
    assert any("source=stdout | quick-ok" in line for line in lines)
    assert any("final status=completed exit_code=0" in line for line in lines)

    # Uniform log schema: timestamp | LEVEL | run/task/panel/source context | message
    assert re.match(
        (
            r"^\d{4}-\d{2}-\d{2}T.*\|\s+[A-Z]+\s+\|\s+"
            r"run_id=\d+\s+task_id=[^\s]+\s+panel_id=[^\s]+\s+source=[^\s]+\s+\|\s+.+$"
        ),
        lines[0],
    )


def test_task_run_artifact_log_captures_startup_exception(
    test_client,
    wait_for_terminal_run,
    tmp_path,
    monkeypatch,
) -> None:
    client, main_app = test_client
    artifacts_root = tmp_path / "_artifacts" / "task_runs"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    main_app.state.runner._artifacts_root = artifacts_root

    def _boom(*_args, **_kwargs):
        raise RuntimeError("popen-boom")

    monkeypatch.setattr(task_runtime.subprocess, "Popen", _boom)

    response = client.post("/api/tasks/shayan.quick/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "failed"
    assert "popen-boom" in str(run.get("error_text") or "")

    run_log_path = artifacts_root / "shayan.quick" / f"run-{run_id}.log"
    assert run_log_path.exists()
    log_text = run_log_path.read_text(encoding="utf-8")
    assert "source=runtime | exception=popen-boom" in log_text


def test_task_logs_are_redacted_in_db_and_artifact_files(
    test_client,
    wait_for_terminal_run,
    tmp_path,
) -> None:
    client, main_app = test_client
    artifacts_root = tmp_path / "_artifacts" / "task_runs"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    main_app.state.runner._artifacts_root = artifacts_root

    main_app.state.db.seed_tasks(
        [
            {
                "task_id": "maintenance.secret_log_redaction",
                "panel_id": "maintenance",
                "title": "Secret log redaction",
                "task_type": "backup",
                "icon_idle": "Play",
                "icon_running": "Square",
                "cwd": ".",
                "command": {
                    "mode": "shell",
                    "value": (
                        "python3 -c \"print('token=abc123 "
                        "aws_secret_access_key=SECRETVALUE "
                        "--repo1-s3-key-secret=SECRETKEY "
                        "--repo1-s3-key=ACCESSKEY "
                        "Authorization: Bearer VERYSECRETTOKEN "
                        "https://example.com/path?token=QUERYTOKEN&x=1 "
                        "https://user:plainpass@example.com/path')\""
                    ),
                },
            }
        ]
    )

    response = client.post("/api/tasks/maintenance.secret_log_redaction/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id)
    assert run["status"] == "completed"

    logs = client.get(f"/api/runs/{run_id}/logs").json()["lines"]
    combined = "\n".join(str(line.get("line") or "") for line in logs)
    assert "<redacted>" in combined
    assert "abc123" not in combined
    assert "SECRETVALUE" not in combined
    assert "SECRETKEY" not in combined
    assert "ACCESSKEY" not in combined
    assert "VERYSECRETTOKEN" not in combined
    assert "QUERYTOKEN" not in combined
    assert "plainpass" not in combined

    run_log_path = artifacts_root / "maintenance.secret_log_redaction" / f"run-{run_id}.log"
    assert run_log_path.exists()
    artifact_text = run_log_path.read_text(encoding="utf-8")
    assert "<redacted>" in artifact_text
    assert "abc123" not in artifact_text
    assert "SECRETVALUE" not in artifact_text
    assert "SECRETKEY" not in artifact_text
    assert "ACCESSKEY" not in artifact_text
    assert "VERYSECRETTOKEN" not in artifact_text
    assert "QUERYTOKEN" not in artifact_text
    assert "plainpass" not in artifact_text


def test_stream_stdout_failures_emit_actionable_log_line(test_client) -> None:
    _client, main_app = test_client
    runner = main_app.state.runner
    task = main_app.state.db.get_task("shayan.quick")
    assert task is not None
    run_id = main_app.state.db.create_run(task)

    class _BoomStream:
        def __init__(self) -> None:
            self._step = 0

        def __iter__(self):
            return self

        def __next__(self) -> str:
            if self._step == 0:
                self._step = 1
                return "line-before-error\n"
            raise RuntimeError("stream exploded")

    class _Proc:
        stdout = _BoomStream()

    runner._stream_stdout_lines(_Proc(), run_id, task["task_id"], task["panel_id"])
    logs = main_app.state.db.get_logs(run_id, after_log_id=0, limit=50)
    combined = "\n".join(str(item.get("line") or "") for item in logs)
    assert "line-before-error" in combined
    assert "log_stream_error=stream exploded" in combined


def test_stream_stdout_closed_file_error_is_ignored(test_client) -> None:
    _client, main_app = test_client
    runner = main_app.state.runner
    task = main_app.state.db.get_task("shayan.quick")
    assert task is not None
    run_id = main_app.state.db.create_run(task)

    class _ClosedStream:
        def __iter__(self):
            return self

        def __next__(self) -> str:
            raise ValueError("I/O operation on closed file")

    class _Proc:
        stdout = _ClosedStream()

    runner._stream_stdout_lines(_Proc(), run_id, task["task_id"], task["panel_id"])
    logs = main_app.state.db.get_logs(run_id, after_log_id=0, limit=50)
    combined = "\n".join(str(item.get("line") or "") for item in logs)
    assert "log_stream_error=" not in combined


def test_task_completion_not_blocked_by_open_stdout_fd(test_client, wait_for_terminal_run) -> None:
    client, main_app = test_client

    main_app.state.db.seed_tasks(
        [
            {
                "task_id": "maintenance.stdout_fd_open",
                "panel_id": "maintenance",
                "title": "stdout fd open",
                "task_type": "backup",
                "icon_idle": "Play",
                "icon_running": "Square",
                "cwd": ".",
                "command": {
                    "mode": "shell",
                    "value": (
                        "python3 -c \"import subprocess,sys; "
                        "subprocess.Popen(['python3','-c','import time; time.sleep(3)'], "
                        "stdout=sys.stdout, stderr=sys.stderr); "
                        "print('parent-exit', flush=True)\""
                    ),
                },
            }
        ]
    )

    response = client.post("/api/tasks/maintenance.stdout_fd_open/toggle")
    assert response.status_code == 200
    run_id = int(response.json()["run"]["run_id"])
    run = wait_for_terminal_run(main_app, run_id, timeout_seconds=3.0)
    assert run["status"] == "completed"


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
