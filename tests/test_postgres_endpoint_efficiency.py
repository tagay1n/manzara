"""Remote-RTT regression coverage for core API payload composition."""

from __future__ import annotations

import time


def _metric_delta(before: dict[str, int], after: dict[str, int], name: str) -> int:
    return int(after[name]) - int(before[name])


def test_core_endpoints_reuse_connections_and_keep_query_counts_bounded(
    test_client,
) -> None:
    client, main_app = test_client

    expectations = {
        "/api/health": {
            "keys": {"status", "time"},
            "queries": 0,
            "checkouts": 0,
        },
        "/api/tasks": {
            "keys": {"generated_at", "event_cursor", "global", "flows", "conveyor"},
            "queries": 5,
            "checkouts": 5,
        },
        "/api/dashboard": {
            "keys": {"generated_at", "event_cursor", "global", "panels", "recent_runs"},
            "queries": 7,
            "checkouts": 7,
        },
        "/api/gemini/state": {
            "keys": {"event_cursor", "gemini"},
            "queries": 6,
            "checkouts": 5,
        },
    }

    # Establish one warm physical connection before measuring endpoint deltas.
    main_app.state.db.get_latest_event_id()
    assert client.get("/api/gemini/state").status_code == 200
    for path, expected in expectations.items():
        before = main_app.state.db.get_pool_metrics()
        started = time.perf_counter()
        response = client.get(path)
        elapsed = time.perf_counter() - started
        after = main_app.state.db.get_pool_metrics()

        assert response.status_code == 200
        assert set(response.json()) == expected["keys"]
        assert _metric_delta(before, after, "queries") == expected["queries"], path
        assert _metric_delta(before, after, "checkouts") == expected["checkouts"], path
        assert _metric_delta(before, after, "physical_connections_created") == 0, path
        assert after["physical_connections_open"] <= after["max_size"] == 4
        print(
            f"{path} elapsed={elapsed:.3f}s "
            f"queries={_metric_delta(before, after, 'queries')} "
            f"checkouts={_metric_delta(before, after, 'checkouts')} "
            f"physical_created={_metric_delta(before, after, 'physical_connections_created')}"
        )

    reset_events = [
        event
        for event in main_app.state.db.get_events_after(0, limit=100)
        if event["type"] == "gemini.all_reset"
    ]
    assert reset_events == []
