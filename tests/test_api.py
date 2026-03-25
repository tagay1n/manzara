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
)
from app.modules.oscar.workflow import OSCAR_PIPELINE_WORKFLOW_ID
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
    assert "oscar" in panels

    shayan = panels["shayan"]
    task_ids = {task["task_id"] for task in shayan["tasks"]}
    assert {"shayan.quick", "shayan.long", "shayan.ignore_sigint"} <= task_ids
    assert {"shayan.scan_changes", "shayan.download_new"} <= task_ids
    assert shayan["workflows"][0]["workflow_id"] == SHAYAN_WEEKLY_WORKFLOW_ID

    maintenance = panels["maintenance"]
    maintenance_task_ids = {task["task_id"] for task in maintenance["tasks"]}
    assert "maintenance.monocorpus_sync" in maintenance_task_ids
    assert "maintenance.pgbackrest_backup_full" in maintenance_task_ids
    assert "maintenance.pgbackrest_backup_incr" in maintenance_task_ids
    assert "maintenance.monocorpus_meta_evaluate" not in maintenance_task_ids

    oscar = panels["oscar"]
    oscar_task_ids = {task["task_id"] for task in oscar["tasks"]}
    assert "oscar.resolve_offsets_local" in oscar_task_ids
    assert "oscar.download_ranges" in oscar_task_ids
    assert "oscar.export_parquet" in oscar_task_ids

    library = panels["library"]
    library_task_ids = {task["task_id"] for task in library["tasks"]}
    assert "maintenance.monocorpus_meta_evaluate" in library_task_ids
    assert "library.collection_detect" in library_task_ids
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
    main_app.state.db.seed_tasks(main_app.oscar_task_definitions(main_app.state.settings.oscar))

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
    assert OSCAR_PIPELINE_WORKFLOW_ID in workflow_ids


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


def test_tasks_endpoint_groups_tasks_by_flow(test_client) -> None:
    client, _main_app = test_client

    response = client.get("/api/tasks")
    assert response.status_code == 200
    payload = response.json()
    flow_ids = {flow["panel_id"] for flow in payload["flows"]}
    assert {"shayan", "maintenance", "oscar", "library"} <= flow_ids

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
        },
    )

    response = client.get("/api/library")
    assert response.status_code == 200
    payload = response.json()
    assert payload["dataset"]["available"] is True
    assert payload["dataset"]["stats"]["applicable_docs"] == 25
    assert payload["dataset"]["top_classifications"][0]["ddc"] == "891.7"


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
    payload = response.json()["gemini"]
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


def test_oscar_pipeline_workflow_runs_three_steps(
    test_client,
    wait_for_terminal_workflow_run,
) -> None:
    client, main_app = test_client

    response = client.post(f"/api/workflows/{OSCAR_PIPELINE_WORKFLOW_ID}/run")
    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "start"
    workflow_run_id = int(payload["workflow_run"]["workflow_run_id"])

    workflow_run = wait_for_terminal_workflow_run(main_app, workflow_run_id)
    assert workflow_run["status"] == "completed"

    step_runs = main_app.state.db.list_workflow_step_runs(workflow_run_id)
    assert [step["task_id"] for step in step_runs] == [
        "oscar.resolve_offsets_local",
        "oscar.download_ranges",
        "oscar.export_parquet",
    ]
    assert all(step["status"] == "completed" for step in step_runs)


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
