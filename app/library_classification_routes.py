"""Library classification API route registration."""

from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.library_route_params import (
    parse_csv_tokens,
    q_limit,
    q_non_negative,
    q_page,
    q_page_size,
    q_ratio,
    q_text,
)


def register_library_classification_routes(
    app: FastAPI,
    *,
    operations_provider: Callable[[], Dict[str, Callable[..., Any]]],
    build_classification_detail_payload: Callable[..., Dict[str, Any]],
) -> None:
    """Register all `/api/library/classifications*` endpoints."""

    @app.get("/api/library/classifications")
    def get_library_classifications(
        search: str = q_text(),
        status: str = q_text(max_length=40),
        ddc_prefix: str = q_text(max_length=40),
        min_usage: int = q_non_negative(),
        page: int = q_page(),
        page_size: int = q_page_size(default=25, max_value=100),
        sort: str = q_text(default="usage_desc", max_length=40),
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
        row_limit: int = q_limit(default=5000, minimum=1, maximum=20000),
        duplicate_limit: int = q_limit(default=25, minimum=1, maximum=200),
        unclassified_limit: int = q_limit(default=30, minimum=1, maximum=200),
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
        drop_segments: str = q_text(default="Turkic literature", max_length=300),
        limit: int = q_limit(default=120, minimum=1, maximum=500),
        row_limit: int = q_limit(default=5000, minimum=1, maximum=20000),
    ) -> JSONResponse:
        """Preview simplification rules before applying any merge."""
        operations = operations_provider()
        segments = parse_csv_tokens(drop_segments)
        payload = operations["get_normalization_preview"](
            drop_segments=segments,
            limit=limit,
            row_limit=row_limit,
        )
        return JSONResponse(payload)

    @app.get("/api/library/classifications/merge-candidates")
    def get_library_classification_merge_candidates(
        limit: int = q_limit(default=80, minimum=1, maximum=300),
        min_score: float = q_ratio(default=0.78),
        row_limit: int = q_limit(default=1200, minimum=10, maximum=10000),
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
        docs_page: int = q_page(default=1),
        docs_page_size: int = q_page_size(default=40, max_value=200),
    ) -> JSONResponse:
        """Return one classification detail."""
        return JSONResponse(
            build_classification_detail_payload(
                classification_id,
                docs_page=docs_page,
                docs_page_size=docs_page_size,
            )
        )
