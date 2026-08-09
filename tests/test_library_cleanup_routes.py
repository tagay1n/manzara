"""Library cleanup API contract tests without external storage."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import library_cleanup_routes


class _FakeDb:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def get_latest_event_id(self) -> int:
        return 42

    def insert_event(self, event_type: str, **kwargs) -> None:  # noqa: ANN003
        self.events.append((event_type, kwargs))


class _FakeRepository:
    def __init__(self, *_args, **_kwargs) -> None:
        self.disposed = False

    def get_overview(self):
        return {
            "active_plans": 2,
            "pending_reviews": 1,
            "failed_plans": 0,
            "completed_plans": 7,
        }

    def list_queue(self, *, status: str, limit: int):
        return [{"cleanup_id": 9, "status": status or "planned", "limit": limit}]

    def list_reviews(self, *, status: str, limit: int):
        return [{"review_id": 3, "status": status, "limit": limit}]

    def dispose(self) -> None:
        self.disposed = True


def test_cleanup_snapshot_exposes_own_event_cursor_and_server_state(monkeypatch) -> None:
    monkeypatch.setattr(
        library_cleanup_routes, "DocumentCleanupRepository", _FakeRepository
    )
    state = SimpleNamespace(
        settings=SimpleNamespace(database_url="postgresql://unused", database_schema="test"),
        db=_FakeDb(),
    )
    app = FastAPI()
    library_cleanup_routes.register_library_cleanup_routes(app, state_provider=lambda: state)
    client = TestClient(app)

    snapshot = client.get("/api/library/document-cleanup")
    queue = client.get("/api/library/document-cleanup/queue?status=failed&limit=25")
    reviews = client.get("/api/library/document-cleanup/isbn-reviews")

    assert snapshot.status_code == 200
    assert snapshot.json()["event_cursor"] == 42
    assert snapshot.json()["stats"]["active_plans"] == 2
    assert queue.json()["items"] == [{"cleanup_id": 9, "status": "failed", "limit": 25}]
    assert reviews.json()["items"][0]["status"] == "pending"


def test_cleanup_api_rejects_invalid_review_selection(monkeypatch) -> None:
    monkeypatch.setattr(
        library_cleanup_routes, "DocumentCleanupRepository", _FakeRepository
    )
    state = SimpleNamespace(
        settings=SimpleNamespace(database_url="postgresql://unused", database_schema="test"),
        db=_FakeDb(),
    )
    app = FastAPI()
    library_cleanup_routes.register_library_cleanup_routes(app, state_provider=lambda: state)
    response = TestClient(app).post(
        "/api/library/document-cleanup/isbn-reviews/1/decision",
        json={"keep_md5s": "not-a-list"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "keep_md5s must be an array of strings"
