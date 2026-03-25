"""Library normalization API route registration."""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from app.contracts import NormalizationOperations
from app.library_route_params import q_limit, q_non_negative, q_page, q_page_size, q_ratio, q_text


def register_library_normalization_routes(
    app: FastAPI,
    *,
    state_provider: Callable[[], Any],
    normalization_entity_types: Iterable[str],
    build_normalization_payload: Callable[[str], Dict[str, Any]],
    operations_provider: Callable[[], NormalizationOperations],
) -> None:
    """Register all `/api/library/normalization/*` endpoints."""
    allowed_entities = {str(item).strip().lower() for item in normalization_entity_types}

    def _require_normalization_entity(entity_type: str) -> str:
        normalized = str(entity_type or "").strip().lower()
        if normalized not in allowed_entities:
            raise HTTPException(status_code=404, detail="Normalization entity type not found")
        return normalized

    @app.get("/api/library/normalization/{entity_type}")
    def get_library_normalization(entity_type: str) -> JSONResponse:
        """Return normalization workbench summary payload."""
        normalized = _require_normalization_entity(entity_type)
        return JSONResponse(build_normalization_payload(normalized))

    @app.get("/api/library/normalization/{entity_type}/queue")
    def get_library_normalization_queue(
        entity_type: str,
        status: str = q_text(default="all", max_length=40),
        search: str = q_text(),
        script_label: str = q_text(max_length=40),
        min_docs: int = q_non_negative(),
        page: int = q_page(),
        page_size: int = q_page_size(default=40, max_value=200),
    ) -> JSONResponse:
        """Return normalization review queue."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        payload = operations.get_review_queue(
            state.db,
            normalized,
            status=status,
            search=search,
            script_label=script_label,
            min_docs=min_docs,
            page=page,
            page_size=page_size,
        )
        return JSONResponse(payload)

    @app.get("/api/library/normalization/{entity_type}/canonicals")
    def get_library_normalization_canonicals(
        entity_type: str,
        search: str = q_text(max_length=160),
    ) -> JSONResponse:
        """Return canonical registry entries for normalization entity."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        return JSONResponse(operations.list_canonicals(state.db, normalized, search=search))

    @app.post("/api/library/normalization/{entity_type}/canonicals")
    def create_library_normalization_canonical(
        entity_type: str,
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Create one canonical entry."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        display_name = str(payload.get("display_name") or "").strip()
        notes = str(payload.get("notes") or "").strip()
        if not display_name:
            raise HTTPException(status_code=400, detail="display_name is required")
        try:
            result = operations.create_canonical(
                state.db,
                normalized,
                display_name=display_name,
                notes=notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.post("/api/library/normalization/{entity_type}/decisions/link")
    def link_library_normalization_alias(
        entity_type: str,
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Link alias to canonical."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        raw_name = str(payload.get("raw_name") or "").strip()
        if not raw_name:
            raise HTTPException(status_code=400, detail="raw_name is required")
        canonical_id = payload.get("canonical_id")
        try:
            canonical_int = int(canonical_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="canonical_id must be an integer")

        confidence_raw = payload.get("confidence")
        confidence = None
        if confidence_raw is not None and str(confidence_raw).strip() != "":
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="confidence must be a number")

        suggestion_ids_raw = payload.get("suggestion_ids") or []
        suggestion_ids = [int(item) for item in suggestion_ids_raw if str(item).strip()]

        try:
            result = operations.link_alias(
                state.db,
                normalized,
                raw_name=raw_name,
                canonical_id=canonical_int,
                source=str(payload.get("source") or "manual"),
                confidence=confidence if confidence is not None else 1.0,
                reason=str(payload.get("reason") or ""),
                suggestion_ids=suggestion_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.post("/api/library/normalization/{entity_type}/decisions/create-link")
    def create_and_link_library_normalization_alias(
        entity_type: str,
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Create canonical and link alias in one action."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        raw_name = str(payload.get("raw_name") or "").strip()
        display_name = str(payload.get("display_name") or "").strip()
        suggestion_ids_raw = payload.get("suggestion_ids") or []
        suggestion_ids = [int(item) for item in suggestion_ids_raw if str(item).strip()]
        if not raw_name:
            raise HTTPException(status_code=400, detail="raw_name is required")
        if not display_name:
            raise HTTPException(status_code=400, detail="display_name is required")

        try:
            result = operations.create_and_link_alias(
                state.db,
                normalized,
                raw_name=raw_name,
                display_name=display_name,
                reason=str(payload.get("reason") or ""),
                suggestion_ids=suggestion_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.post("/api/library/normalization/{entity_type}/decisions/reject")
    def reject_library_normalization_alias(
        entity_type: str,
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Reject alias from normalization queue."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        raw_name = str(payload.get("raw_name") or "").strip()
        if not raw_name:
            raise HTTPException(status_code=400, detail="raw_name is required")
        suggestion_ids_raw = payload.get("suggestion_ids") or []
        suggestion_ids = [int(item) for item in suggestion_ids_raw if str(item).strip()]

        try:
            result = operations.reject_alias(
                state.db,
                normalized,
                raw_name=raw_name,
                reason=str(payload.get("reason") or ""),
                suggestion_ids=suggestion_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.post("/api/library/normalization/{entity_type}/bulk/link")
    def bulk_link_library_normalization_aliases(
        entity_type: str,
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Bulk-link aliases to a canonical."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        raw_names = payload.get("raw_names") or []
        canonical_id = payload.get("canonical_id")
        try:
            canonical_int = int(canonical_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="canonical_id must be an integer")

        try:
            result = operations.bulk_link_aliases(
                state.db,
                normalized,
                raw_names=[str(item) for item in raw_names],
                canonical_id=canonical_int,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.post("/api/library/normalization/{entity_type}/bulk/reject")
    def bulk_reject_library_normalization_aliases(
        entity_type: str,
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Bulk-reject aliases from queue."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        raw_names = payload.get("raw_names") or []
        try:
            result = operations.bulk_reject_aliases(
                state.db,
                normalized,
                raw_names=[str(item) for item in raw_names],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.get("/api/library/normalization/{entity_type}/suggestions")
    def get_library_normalization_suggestions(
        entity_type: str,
        limit: int = q_limit(default=200, minimum=1, maximum=1000),
    ) -> JSONResponse:
        """Return open suggestions for normalization queue."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        return JSONResponse(operations.list_suggestions(state.db, normalized, limit=limit))

    @app.post("/api/library/normalization/{entity_type}/suggestions/refresh")
    def refresh_library_normalization_suggestions(
        entity_type: str,
        payload: Dict[str, Any] = Body(default={}),
    ) -> JSONResponse:
        """Regenerate normalization suggestions."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        limit = payload.get("limit", 120)
        use_gemini = payload.get("use_gemini", True)
        try:
            result = operations.refresh_suggestions(
                state.db,
                normalized,
                limit=int(limit),
                use_gemini=bool(use_gemini),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.get("/api/library/normalization/{entity_type}/merge-candidates")
    def get_library_normalization_merge_candidates(
        entity_type: str,
        min_score: float = q_ratio(default=0.84),
        limit: int = q_limit(default=80, minimum=1, maximum=300),
    ) -> JSONResponse:
        """Return possible canonical merge candidates."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        return JSONResponse(
            operations.get_normalization_merge_candidates(
                state.db,
                normalized,
                min_score=min_score,
                limit=limit,
            )
        )

    @app.post("/api/library/normalization/{entity_type}/merge")
    def merge_library_normalization_canonicals(
        entity_type: str,
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Merge source canonical into target canonical."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        try:
            source_canonical_id = int(payload.get("source_canonical_id"))
            target_canonical_id = int(payload.get("target_canonical_id"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="source_canonical_id and target_canonical_id must be integers",
            )

        try:
            result = operations.merge_canonicals(
                state.db,
                normalized,
                source_canonical_id=source_canonical_id,
                target_canonical_id=target_canonical_id,
                reason=str(payload.get("reason") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.get("/api/library/normalization/{entity_type}/history")
    def get_library_normalization_history(
        entity_type: str,
        limit: int = q_limit(default=200, minimum=1, maximum=1000),
    ) -> JSONResponse:
        """Return normalization action history."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        return JSONResponse(operations.list_normalization_history(state.db, normalized, limit=limit))

    @app.post("/api/library/normalization/{entity_type}/history/{event_id}/undo")
    def undo_library_normalization_history_event(
        entity_type: str,
        event_id: int,
    ) -> JSONResponse:
        """Undo one normalization event by id."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        try:
            result = operations.undo_event(state.db, normalized, event_id=event_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(result)

    @app.get("/api/library/normalization/{entity_type}/quality")
    def get_library_normalization_quality(entity_type: str) -> JSONResponse:
        """Return quality metrics for normalization workbench."""
        state = state_provider()
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        return JSONResponse(operations.get_normalization_quality(state.db, normalized))

    @app.get("/api/library/normalization/{entity_type}/evidence")
    def get_library_normalization_alias_evidence(
        entity_type: str,
        raw_name: str = Query(..., min_length=1, max_length=240),
        limit: int = q_limit(default=20, minimum=1, maximum=200),
    ) -> JSONResponse:
        """Return sample docs where alias appears."""
        operations = operations_provider()
        normalized = _require_normalization_entity(entity_type)
        try:
            payload = operations.get_normalization_evidence(
                normalized,
                raw_name=raw_name,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(payload)
