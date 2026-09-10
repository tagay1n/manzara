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

    def insert_event(
        self,
        event_type: str,
        task_id: str | None,
        run_id: int | None,
        panel_id: str | None,
        payload: dict,
    ) -> None:
        self.events.append(
            (
                event_type,
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "panel_id": panel_id,
                    "payload": payload,
                },
            )
        )


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

    def undo_review(self, review_id: int):
        return {
            "review_id": review_id,
            "status": "pending",
            "canceled_cleanup_ids": [9],
        }

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


def test_cleanup_api_decision_emits_refresh_event_without_failing_response(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        library_cleanup_routes, "DocumentCleanupRepository", _FakeRepository
    )
    monkeypatch.setattr(library_cleanup_routes, "load_runtime_config", dict)
    monkeypatch.setattr(
        library_cleanup_routes,
        "load_document_storage_settings",
        lambda _config: SimpleNamespace(
            filtered_out_path="/filtered",
            source_path="/documents",
        ),
    )
    monkeypatch.setattr(
        library_cleanup_routes,
        "apply_isbn_review_decision",
        lambda **_kwargs: {
            "review_id": 3,
            "isbn": "9780306406157",
            "keep_md5s": ["a" * 32],
            "remove_candidates": [],
            "queued": 0,
        },
    )
    state = SimpleNamespace(
        settings=SimpleNamespace(database_url="postgresql://unused", database_schema="test"),
        db=_FakeDb(),
    )
    app = FastAPI()
    library_cleanup_routes.register_library_cleanup_routes(app, state_provider=lambda: state)

    response = TestClient(app).post(
        "/api/library/document-cleanup/isbn-reviews/3/decision",
        json={"keep_md5s": ["a" * 32]},
    )

    assert response.status_code == 200
    assert response.json()["queued"] == 0
    assert state.db.events[-1] == (
        "library.document_cleanup_changed",
        {
            "task_id": None,
            "run_id": None,
            "panel_id": "library",
            "payload": {"review_id": 3, "queued": 0},
        },
    )


def test_cleanup_api_undoes_review_and_emits_change_event(monkeypatch) -> None:
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
        "/api/library/document-cleanup/isbn-reviews/3/undo"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert state.db.events[-1][0] == "library.document_cleanup_changed"
    assert state.db.events[-1][1]["payload"] == {
        "review_id": 3,
        "action": "undo",
        "canceled_cleanup_ids": [9],
    }


def test_cleanup_api_reports_unsafe_undo_as_conflict(monkeypatch) -> None:
    class _UnsafeUndoRepository(_FakeRepository):
        def undo_review(self, review_id: int):
            raise ValueError(
                "Cleanup has already started; this ISBN decision can no longer be undone"
            )

    monkeypatch.setattr(
        library_cleanup_routes, "DocumentCleanupRepository", _UnsafeUndoRepository
    )
    state = SimpleNamespace(
        settings=SimpleNamespace(database_url="postgresql://unused", database_schema="test"),
        db=_FakeDb(),
    )
    app = FastAPI()
    library_cleanup_routes.register_library_cleanup_routes(app, state_provider=lambda: state)

    response = TestClient(app).post(
        "/api/library/document-cleanup/isbn-reviews/3/undo"
    )

    assert response.status_code == 409
    assert "already started" in response.json()["detail"]
    assert state.db.events == []
