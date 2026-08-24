"""Library PDF preview API routes."""

from __future__ import annotations

import re
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.modules.library.preview_repository import LibraryPreviewRepository
from app.modules.library.previews import PREVIEW_RECIPE_VERSION, build_preview_api_payload
from app.document_storage import load_document_storage_settings
from app.runtime_config import load_runtime_config


_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def get_book_preview_storage() -> tuple[str, str, str]:
    """Resolve public document eligibility and preview URL storage settings."""
    storage = load_document_storage_settings(load_runtime_config())
    return (
        storage.primary.endpoint_url,
        storage.public_bucket,
        storage.preview_bucket,
    )


def register_library_preview_routes(
    app: FastAPI,
    *,
    state_provider: Callable[[], Any],
) -> None:
    """Register semantic read APIs for generated book previews."""

    @app.get("/api/library/previews/{md5}")
    def get_library_book_previews(md5: str) -> JSONResponse:
        digest = str(md5 or "").strip().lower()
        if not _MD5_RE.fullmatch(digest):
            raise HTTPException(status_code=400, detail="md5 must be a 32-character hexadecimal digest")
        state = state_provider()
        repository = LibraryPreviewRepository(
            state.settings.database_url,
            schema=state.settings.database_schema,
        )
        try:
            endpoint_url, public_bucket, preview_bucket = get_book_preview_storage()
            if not repository.is_eligible_pdf(
                digest,
                endpoint_url=endpoint_url,
                public_bucket=public_bucket,
            ):
                raise HTTPException(status_code=404, detail="Applicable PDF not found")
            row = repository.get(digest) or {
                "md5": digest,
                "status": "pending",
                "recipe_version": PREVIEW_RECIPE_VERSION,
                "source_page_count": None,
                "error_text": None,
            }
            return JSONResponse(
                build_preview_api_payload(
                    row,
                    bucket=preview_bucket,
                    endpoint_url=endpoint_url,
                )
            )
        finally:
            repository.dispose()


__all__ = ["get_book_preview_storage", "register_library_preview_routes"]
