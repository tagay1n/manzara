"""Library personalities/publishers/collections API route registration."""

from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.contracts import EntitiesOperations
from app.library_route_params import q_limit, q_non_negative, q_page, q_page_size, q_text


def register_library_entities_routes(
    app: FastAPI,
    *,
    state_provider: Callable[[], Any],
    operations_provider: Callable[[], EntitiesOperations],
    build_personality_payload: Callable[[], Dict[str, Any]],
    build_publisher_payload: Callable[[], Dict[str, Any]],
    build_collections_payload: Callable[[], Dict[str, Any]],
) -> None:
    """Register personalities/publishers/collections endpoints."""

    @app.get("/api/library/personalities")
    def get_library_personalities() -> JSONResponse:
        """Return personality overview payload."""
        return JSONResponse(build_personality_payload())

    @app.get("/api/library/personalities/table")
    def get_library_personalities_table(
        search: str = q_text(),
        script_label: str = q_text(max_length=40),
        min_docs: int = q_non_negative(),
        page: int = q_page(),
        page_size: int = q_page_size(default=25, max_value=100),
        sort: str = q_text(default="docs_desc", max_length=40),
    ) -> JSONResponse:
        """Return paginated personalities table."""
        operations = operations_provider()
        payload = operations.list_personalities(
            search=search,
            script_label=script_label,
            min_docs=min_docs,
            page=page,
            page_size=page_size,
            sort=sort,
        )
        return JSONResponse(payload)

    @app.get("/api/library/personalities/insights")
    def get_library_personalities_insights(
        cluster_limit: int = q_limit(default=24, minimum=1, maximum=100),
        queue_limit: int = q_limit(default=40, minimum=1, maximum=200),
    ) -> JSONResponse:
        """Return personalities insight tabs payload."""
        operations = operations_provider()
        payload = operations.get_personality_insights(
            cluster_limit=cluster_limit,
            queue_limit=queue_limit,
        )
        return JSONResponse(payload)

    @app.get("/api/library/publishers")
    def get_library_publishers() -> JSONResponse:
        """Return publisher overview payload."""
        return JSONResponse(build_publisher_payload())

    @app.get("/api/library/publishers/table")
    def get_library_publishers_table(
        search: str = q_text(),
        script_label: str = q_text(max_length=40),
        min_docs: int = q_non_negative(),
        page: int = q_page(),
        page_size: int = q_page_size(default=25, max_value=100),
        sort: str = q_text(default="docs_desc", max_length=40),
    ) -> JSONResponse:
        """Return paginated publishers table."""
        operations = operations_provider()
        payload = operations.list_publishers(
            search=search,
            script_label=script_label,
            min_docs=min_docs,
            page=page,
            page_size=page_size,
            sort=sort,
        )
        return JSONResponse(payload)

    @app.get("/api/library/publishers/insights")
    def get_library_publishers_insights(
        cluster_limit: int = q_limit(default=24, minimum=1, maximum=100),
        queue_limit: int = q_limit(default=40, minimum=1, maximum=200),
    ) -> JSONResponse:
        """Return publishers insight tabs payload."""
        operations = operations_provider()
        payload = operations.get_publisher_insights(
            cluster_limit=cluster_limit,
            queue_limit=queue_limit,
        )
        return JSONResponse(payload)

    @app.get("/api/library/collections")
    def get_library_collections() -> JSONResponse:
        """Return collection overview payload."""
        return JSONResponse(build_collections_payload())

    @app.get("/api/library/collections/table")
    def get_library_collections_table(
        search: str = q_text(),
        status: str = q_text(max_length=40),
        include: str = q_text(default="all", max_length=10),
        page: int = q_page(),
        page_size: int = q_page_size(default=25, max_value=100),
        sort: str = q_text(default="updated_desc", max_length=40),
    ) -> JSONResponse:
        """Return paginated collections table."""
        operations = operations_provider()
        payload = operations.list_library_collections(
            search=search,
            status=status,
            include=include,
            page=page,
            page_size=page_size,
            sort=sort,
        )
        return JSONResponse(payload)

    @app.get("/api/library/collections/insights")
    def get_library_collections_insights(
        cluster_limit: int = q_limit(default=24, minimum=1, maximum=200),
        queue_limit: int = q_limit(default=40, minimum=1, maximum=200),
    ) -> JSONResponse:
        """Return collection insight tabs payload."""
        operations = operations_provider()
        payload = operations.get_collection_insights(
            cluster_limit=cluster_limit,
            queue_limit=queue_limit,
        )
        return JSONResponse(payload)

    @app.get("/api/library/collections/{collection_id}/items")
    def get_library_collection_items(
        collection_id: int,
        limit: int = q_limit(default=400, minimum=1, maximum=2000),
    ) -> JSONResponse:
        """Return one collection with linked items."""
        operations = operations_provider()
        payload = operations.list_collection_items(collection_id, limit=limit)
        return JSONResponse(payload)

    @app.get("/api/library/collections/{collection_id}/review")
    def get_library_collection_review(
        collection_id: int,
        sample_limit: int = q_limit(default=8, minimum=3, maximum=20),
        outlier_limit: int = q_limit(default=20, minimum=1, maximum=100),
    ) -> JSONResponse:
        """Return aggregate evidence and bounded examples for review."""
        operations = operations_provider()
        payload = operations.get_collection_review(
            collection_id,
            sample_limit=sample_limit,
            outlier_limit=outlier_limit,
        )
        return JSONResponse(payload)

    @app.patch("/api/library/collections/{collection_id}")
    def patch_library_collection(
        collection_id: int,
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Patch collection review status/title/notes/include settings."""
        state = state_provider()
        operations = operations_provider()
        try:
            result = operations.update_collection(state.db, collection_id, updates=payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.post("/api/library/collections/{collection_id}/merge")
    def merge_library_collection(
        collection_id: int,
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Merge one detected collection into a canonical collection."""
        target_id = payload.get("target_collection_id")
        if isinstance(target_id, bool) or not isinstance(target_id, int):
            raise HTTPException(status_code=400, detail="target_collection_id must be an integer")
        state = state_provider()
        operations = operations_provider()
        try:
            result = operations.merge_collections(
                state.db,
                source_collection_id=collection_id,
                target_collection_id=target_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)
