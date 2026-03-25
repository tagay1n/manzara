"""Library classification API route registration."""

from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse


def register_library_classification_routes(
    app: FastAPI,
    *,
    operations_provider: Callable[[], Dict[str, Callable[..., Any]]],
    build_classification_detail_payload: Callable[..., Dict[str, Any]],
) -> None:
    """Register all `/api/library/classifications*` endpoints."""

    @app.get("/api/library/classifications")
    def get_library_classifications(
        search: str = Query("", max_length=120),
        status: str = Query("", max_length=40),
        ddc_prefix: str = Query("", max_length=40),
        min_usage: int = Query(0, ge=0),
        page: int = Query(1, ge=1),
        page_size: int = Query(25, ge=1, le=100),
        sort: str = Query("usage_desc", max_length=40),
    ) -> JSONResponse:
        """Return paginated classification table."""
        operations = operations_provider()
        payload = operations["list_classifications"](
            search=search,
            status=status,
            ddc_prefix=ddc_prefix,
            min_usage=min_usage,
            page=page,
            page_size=page_size,
            sort=sort,
        )
        return JSONResponse(payload)

    @app.get("/api/library/classifications/insights")
    def get_library_classification_insights(
        row_limit: int = Query(5000, ge=1, le=20000),
        duplicate_limit: int = Query(25, ge=1, le=200),
        unclassified_limit: int = Query(30, ge=1, le=200),
    ) -> JSONResponse:
        """Return hierarchy, distribution, duplicates, and unclassified queue."""
        operations = operations_provider()
        payload = operations["get_classification_insights"](
            row_limit=row_limit,
            duplicate_limit=duplicate_limit,
            unclassified_limit=unclassified_limit,
        )
        return JSONResponse(payload)

    @app.get("/api/library/classifications/normalization-preview")
    def get_library_classification_normalization_preview(
        drop_segments: str = Query("Turkic literature", max_length=300),
        limit: int = Query(120, ge=1, le=500),
        row_limit: int = Query(5000, ge=1, le=20000),
    ) -> JSONResponse:
        """Preview simplification rules before applying any merge."""
        operations = operations_provider()
        segments = [item.strip() for item in drop_segments.split(",") if item.strip()]
        payload = operations["get_normalization_preview"](
            drop_segments=segments,
            limit=limit,
            row_limit=row_limit,
        )
        return JSONResponse(payload)

    @app.get("/api/library/classifications/merge-candidates")
    def get_library_classification_merge_candidates(
        limit: int = Query(80, ge=1, le=300),
        min_score: float = Query(0.78, ge=0.0, le=1.0),
        row_limit: int = Query(1200, ge=10, le=10000),
    ) -> JSONResponse:
        """Return ranked near-duplicate classification merge suggestions."""
        operations = operations_provider()
        payload = operations["get_merge_candidates"](
            limit=limit,
            min_score=min_score,
            row_limit=row_limit,
        )
        return JSONResponse(payload)

    @app.get("/api/library/classifications/{classification_id}")
    def get_library_classification_detail(
        classification_id: int,
        docs_page: int = Query(1, ge=1),
        docs_page_size: int = Query(40, ge=1, le=200),
    ) -> JSONResponse:
        """Return one classification detail."""
        return JSONResponse(
            build_classification_detail_payload(
                classification_id,
                docs_page=docs_page,
                docs_page_size=docs_page_size,
            )
        )
