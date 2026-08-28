"""API/runtime behavior tests for Manzara."""

from __future__ import annotations

import time

import pytest
from app.gemini_config import GeminiKey
from app.gemini_runtime import GeminiRequestRejectedError, GeminiRuntimeManager


def _wait_for_status(main_app, run_id: int, expected: set[str], timeout_seconds: float = 4.0):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        run = main_app.state.db.get_run(run_id)
        if run and run["status"] in expected:
            return run
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not reach expected status: {expected}")


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


def test_gemini_state_reports_capacity_for_every_configured_model(
    test_client, monkeypatch
) -> None:
    client, main_app = test_client
    keys = [
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
    ]
    monkeypatch.setattr("app.gemini_runtime.load_gemini_keys", lambda: keys)
    monkeypatch.setattr(
        "app.gemini_runtime.load_configured_gemini_model_names",
        lambda: ["gemini-a", "gemini-b"],
    )

    main_app.state.db.upsert_gemini_keys(
        [
            {
                "account_id": key.account_id,
                "key_id": key.key_id,
                "masked_key": key.masked_key,
            }
            for key in keys
        ]
    )
    for key in keys:
        for model_name in ("gemini-a", "gemini-b"):
            main_app.state.db.ensure_gemini_model_state(key.key_id, model_name)
    main_app.state.db.mark_gemini_error(
        "acc-a:key-1",
        "gemini-a",
        now_ts="2026-08-27T10:00:00+00:00",
        error_text="quota details should not be needed by the page",
        exhausted=True,
    )

    response = client.get("/api/gemini/state")

    assert response.status_code == 200
    payload = response.json()["gemini"]
    assert payload["configured_models"] == ["gemini-a", "gemini-b"]
    usage = {item["model_name"]: item for item in payload["model_usage"]}
    assert usage["gemini-a"] == {
        "model_name": "gemini-a",
        "total_keys": 2,
        "available_keys": 1,
        "exhausted_keys": 1,
        "usage_percent": 50,
        "attempts_cycle": 0,
        "success_cycle": 0,
    }
    assert usage["gemini-b"]["usage_percent"] == 0
    assert all(
        [model["model_name"] for model in key["models"]]
        == ["gemini-a", "gemini-b"]
        for key in payload["accounts"][0]["keys"]
    )


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
    db.ensure_gemini_model_state("acc-a:key-1", "gemini-test")
    db.ensure_gemini_model_state("acc-a:key-2", "gemini-test")
    now_ts = "2026-03-25T00:00:00+00:00"
    db.mark_gemini_error(
        "acc-a:key-1",
        "gemini-test",
        now_ts=now_ts,
        error_text="quota",
        exhausted=True,
    )
    db.mark_gemini_error(
        "acc-a:key-2",
        "gemini-test",
        now_ts=now_ts,
        error_text="quota",
        exhausted=True,
    )

    one = client.post("/api/gemini/reset-key", json={"key_id": "acc-a:key-1"})
    assert one.status_code == 200
    assert one.json()["rows_changed"] >= 1

    rows_after_one = db.list_gemini_model_states(model_name="gemini-test")
    by_key = {str(item["key_id"]): bool(item.get("exhausted")) for item in rows_after_one if item.get("model_name")}
    assert by_key["acc-a:key-1"] is False
    assert by_key["acc-a:key-2"] is True

    all_resp = client.post("/api/gemini/reset-all")
    assert all_resp.status_code == 200
    assert all_resp.json()["rows_changed"] >= 1

    rows_after_all = db.list_gemini_model_states(model_name="gemini-test")
    assert all(bool(item.get("exhausted")) is False for item in rows_after_all if item.get("model_name"))


def test_gemini_reset_key_rejects_missing_or_blank_key_id(test_client) -> None:
    client, _main_app = test_client

    missing = client.post("/api/gemini/reset-key", json={})
    assert missing.status_code == 400
    assert missing.json()["detail"] == "key_id is required"

    blank = client.post("/api/gemini/reset-key", json={"key_id": "   "})
    assert blank.status_code == 400
    assert blank.json()["detail"] == "key_id is required"


def test_gemini_blackout_override_endpoint(test_client, monkeypatch) -> None:
    client, _main_app = test_client
    monkeypatch.setattr(
        "app.control_routes.GeminiRuntimeManager.override_blackout",
        lambda _self: {
            "blackout_override_until": "2026-08-12T08:00:00+00:00",
            "cycle_label": "2026-08-12",
        },
    )

    response = client.post("/api/gemini/override-blackout")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "blackout_override_until": "2026-08-12T08:00:00+00:00",
        "cycle_label": "2026-08-12",
    }


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
            model_name="gemini-test",
            call=_raise_400,
            max_attempts=2,
        )

    rows = main_app.state.db.list_gemini_model_states(model_name="gemini-test")
    assert len(rows) == 1
    row = rows[0]
    assert bool(row.get("exhausted")) is False
    assert row.get("last_error_text") in (None, "")

    control = main_app.state.db.ensure_gemini_runtime_control("2026-03-25")
    assert control.get("pause_until") is None


def test_gemini_worker_override_is_shared_and_consumed_by_next_run(test_client) -> None:
    client, main_app = test_client
    task_id = "library.metadata_extract"

    response = client.patch(
        f"/api/tasks/{task_id}/gemini-workers", json={"workers": 2}
    )
    assert response.status_code == 200

    tasks_payload = client.get("/api/tasks").json()
    task = next(
        item
        for flow in tasks_payload["flows"]
        for item in flow["tasks"]
        if item["task_id"] == task_id
    )
    assert task["gemini_workers"]["next_run"] == 2
    assert task["gemini_workers"]["override_pending"] is True

    run_id = main_app.state.db.create_run(main_app.state.db.get_task(task_id))
    assert main_app.state.db.get_run(run_id)["gemini_workers"] == 2
    assert main_app.state.db.get_task(task_id)["gemini_workers_next"] is None
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["task"]["gemini_workers"]["active"] == 2
    assert detail["task"]["gemini_workers"]["editable"] is False


def test_gemini_worker_override_rejects_bool_fraction_and_unsupported_task(test_client) -> None:
    client, _main_app = test_client
    for value in (True, 1.5):
        response = client.patch(
            "/api/tasks/library.metadata_extract/gemini-workers",
            json={"workers": value},
        )
        assert response.status_code == 400
    response = client.patch(
        "/api/tasks/library.generate_book_previews/gemini-workers",
        json={"workers": 1},
    )
    assert response.status_code == 400
