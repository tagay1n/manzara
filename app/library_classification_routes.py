"""Library classification API route registration."""

from __future__ import annotations

from typing import Any, Callable, Dict

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.contracts import ClassificationOperations
from app.library_route_params import q_non_negative, q_page, q_page_size, q_text
from app.modules.library.classification_editor import StaleTaxonomyError


def register_library_classification_routes(
    app: FastAPI,
    *,
    operations_provider: Callable[[], ClassificationOperations],
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
        payload = operations.list_classifications(
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
    def get_library_classification_insights() -> JSONResponse:
        """Return the complete editable hierarchy and DDC distribution."""
        operations = operations_provider()
        payload = operations.get_classification_insights()
        return JSONResponse(payload)

    @app.get("/api/library/classifications/documents")
    def get_library_classification_documents(
        classification_ids: str,
        offset: int = q_non_negative(),
        limit: int = q_page_size(default=10, max_value=50),
    ) -> JSONResponse:
        """Return a bounded page of real documents for classification leaves."""
        try:
            ids = [int(item.strip()) for item in classification_ids.split(",") if item.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="classification_ids must be integers") from exc
        operations = operations_provider()
        try:
            return JSONResponse(
                operations.list_classification_documents(ids, offset=offset, limit=limit)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/library/classifications/change-set/preview")
    def preview_library_classification_change_set(
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        operations = operations_provider()
        try:
            result = operations.preview_change_set(payload)
        except StaleTaxonomyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.post("/api/library/classifications/change-set/apply")
    def apply_library_classification_change_set(
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        operations = operations_provider()
        try:
            result = operations.apply_change_set(payload)
        except StaleTaxonomyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

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
