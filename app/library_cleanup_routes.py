"""Library document cleanup queue and ISBN review API routes."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.document_storage import load_document_storage_settings
from app.modules.library.document_cleanup_repository import DocumentCleanupRepository
from app.modules.library.document_cleanup_service import apply_isbn_review_decision
from app.runtime_config import load_runtime_config


def register_library_cleanup_routes(
    app: FastAPI,
    *,
    state_provider: Callable[[], Any],
) -> None:
    """Register read/review routes; cleanup execution remains task-owned."""

    def repository() -> DocumentCleanupRepository:
        state = state_provider()
        return DocumentCleanupRepository(
            state.settings.database_url,
            schema=state.settings.database_schema,
        )

    @app.get("/api/library/document-cleanup")
    def get_document_cleanup_overview() -> JSONResponse:
        state = state_provider()
        repo = repository()
        try:
            return JSONResponse(
                {
                    "available": True,
                    "event_cursor": state.db.get_latest_event_id(),
                    "stats": repo.get_overview(),
                }
            )
        finally:
            repo.dispose()

    @app.get("/api/library/document-cleanup/queue")
    def get_document_cleanup_queue(
        status: str = "",
        limit: int = 100,
    ) -> JSONResponse:
        allowed = {
            "",
            "planned",
            "running",
            "completed",
            "failed",
            "canceled",
            "recovered",
        }
        if status not in allowed:
            raise HTTPException(status_code=400, detail="Unsupported cleanup status")
        repo = repository()
        try:
            return JSONResponse(
                jsonable_encoder(
                    {"available": True, "items": repo.list_queue(status=status, limit=limit)}
                )
            )
        finally:
            repo.dispose()

    @app.get("/api/library/document-cleanup/isbn-reviews")
    def get_document_cleanup_reviews(
        status: str = "pending",
        limit: int = 100,
    ) -> JSONResponse:
        if status not in {"", "pending", "decided", "superseded"}:
            raise HTTPException(status_code=400, detail="Unsupported review status")
        repo = repository()
        try:
            return JSONResponse(
                jsonable_encoder(
                    {"available": True, "items": repo.list_reviews(status=status, limit=limit)}
                )
            )
        finally:
            repo.dispose()

    @app.post("/api/library/document-cleanup/isbn-reviews/{review_id}/decision")
    def decide_document_cleanup_review(
        review_id: int,
        payload: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        keep_md5s = payload.get("keep_md5s")
        if not isinstance(keep_md5s, list) or any(
            not isinstance(value, str) for value in keep_md5s
        ):
            raise HTTPException(status_code=400, detail="keep_md5s must be an array of strings")
        storage = load_document_storage_settings(load_runtime_config())
        repo = repository()
        try:
            result = apply_isbn_review_decision(
                repository=repo,
                review_id=review_id,
                keep_md5s=keep_md5s,
                filtered_out_path=storage.filtered_out_path,
                source_root_path=storage.source_path,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            repo.dispose()
        state_provider().db.insert_event(
            "library.document_cleanup_changed",
            panel_id="library",
            payload={"review_id": review_id, "queued": result["queued"]},
        )
        return JSONResponse(result)


__all__ = ["register_library_cleanup_routes"]
