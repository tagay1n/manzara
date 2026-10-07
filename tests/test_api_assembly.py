"""Database-free regression coverage for the backend application boundary."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import json

from starlette.routing import Match

from app.factory import create_manzara_app


def test_backend_assembly_serves_api_without_frontend_assets():
    @asynccontextmanager
    async def lifespan(app):
        yield

    def unused_provider():
        raise AssertionError("Assembly must not initialize domain services")

    payloads = SimpleNamespace(
        build_system_state_payload=lambda: {"status": "ready"},
        build_dashboard_payload=lambda: {},
        build_tasks_payload=lambda: {"tasks": []},
        build_task_detail_payload=lambda *args, **kwargs: {},
        build_library_payload=lambda: {},
        build_database_state_payload=lambda: {},
    )
    result = create_manzara_app(
        lifespan=lifespan,
        state_provider=unused_provider,
        normalization_entity_types=("personality", "publisher"),
        title_max_length=80,
        sse_poll_interval_seconds=1.0,
        sse_heartbeat_every_empty_polls=15,
        payload_provider=lambda: payloads,
        normalization_operations_provider=unused_provider,
        classification_operations_provider=unused_provider,
        entities_operations_provider=unused_provider,
    )

    routes = {route.path: route for route in result.app.routes}
    assert json.loads(routes["/api/system/state"].endpoint().body) == {"status": "ready"}
    assert json.loads(routes["/api/tasks"].endpoint().body) == {"tasks": []}
    for path in (
        "/", "/dashboard", "/tasks", "/tasks/example", "/database",
        "/gemini", "/library", "/library/classifications",
        "/library/classifications/1", "/library/personalities",
        "/library/publishers", "/library/collections",
        "/library/isbn-conflicts", "/library/normalization/personality",
        "/static/styles.css",
    ):
        scope = {"type": "http", "method": "GET", "path": path, "root_path": ""}
        assert all(route.matches(scope)[0] == Match.NONE for route in result.app.routes)

    paths = {route.path for route in result.app.routes}
    assert "/api/events/stream" in paths
    assert "/api/tasks/{task_id}/toggle" in paths
    assert set(result.stream_handlers) == {"run_logs", "events_stream"}
