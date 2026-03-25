"""Library personalities/publishers/collections API route registration."""

from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse


def register_library_entities_routes(
    app: FastAPI,
    *,
    state_provider: Callable[[], Any],
    operations_provider: Callable[[], Dict[str, Callable[..., Any]]],
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
        search: str = Query("", max_length=120),
        script_label: str = Query("", max_length=40),
        min_docs: int = Query(0, ge=0),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
        sort: str = Query("docs_desc", max_length=40),
    ) -> JSONResponse:
        """Return paginated personalities table."""
        operations = operations_provider()
        payload = operations["list_personalities"](
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
        cluster_limit: int = Query(24, ge=1, le=100),
        queue_limit: int = Query(40, ge=1, le=200),
    ) -> JSONResponse:
        """Return personalities insight tabs payload."""
        operations = operations_provider()
        payload = operations["get_personality_insights"](
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
        search: str = Query("", max_length=120),
        script_label: str = Query("", max_length=40),
        min_docs: int = Query(0, ge=0),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
        sort: str = Query("docs_desc", max_length=40),
    ) -> JSONResponse:
        """Return paginated publishers table."""
        operations = operations_provider()
        payload = operations["list_publishers"](
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
        cluster_limit: int = Query(24, ge=1, le=100),
        queue_limit: int = Query(40, ge=1, le=200),
    ) -> JSONResponse:
        """Return publishers insight tabs payload."""
        operations = operations_provider()
        payload = operations["get_publisher_insights"](
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
        search: str = Query("", max_length=120),
        status: str = Query("", max_length=40),
        include: str = Query("all", max_length=10),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
        sort: str = Query("updated_desc", max_length=40),
    ) -> JSONResponse:
        """Return paginated collections table."""
        operations = operations_provider()
        payload = operations["list_library_collections"](
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
        cluster_limit: int = Query(24, ge=1, le=200),
        queue_limit: int = Query(40, ge=1, le=200),
    ) -> JSONResponse:
        """Return collection insight tabs payload."""
        operations = operations_provider()
        payload = operations["get_collection_insights"](
            cluster_limit=cluster_limit,
            queue_limit=queue_limit,
        )
        return JSONResponse(payload)

    @app.get("/api/library/collections/{collection_id}/items")
    def get_library_collection_items(
        collection_id: int,
        limit: int = Query(400, ge=1, le=2000),
    ) -> JSONResponse:
        """Return one collection with linked items."""
        operations = operations_provider()
        payload = operations["list_collection_items"](collection_id, limit=limit)
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
            result = operations["update_collection"](state.db, collection_id, updates=payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)
