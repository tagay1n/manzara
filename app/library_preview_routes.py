"""Library PDF preview API routes."""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.modules.library.preview_repository import LibraryPreviewRepository
from app.modules.library.previews import PREVIEW_RECIPE_VERSION, build_preview_api_payload
from app.runtime_config import load_runtime_config


_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def get_book_preview_bucket() -> str:
    """Resolve the one configured public preview bucket."""
    config = load_runtime_config()
    buckets = _mapping(_mapping(_mapping(config.get("yandex")).get("cloud")).get("bucket"))
    bucket = str(buckets.get("book_previews") or "").strip()
    if not bucket:
        raise RuntimeError("Missing required config value: yandex.cloud.bucket.book_previews")
    return bucket


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
            if not repository.is_eligible_pdf(digest):
                raise HTTPException(status_code=404, detail="Applicable PDF not found")
            row = repository.get(digest) or {
                "md5": digest,
                "status": "pending",
                "recipe_version": PREVIEW_RECIPE_VERSION,
                "source_page_count": None,
                "manifest": {},
                "error_text": None,
            }
            return JSONResponse(
                build_preview_api_payload(row, bucket=get_book_preview_bucket())
            )
        finally:
            repository.dispose()


__all__ = ["get_book_preview_bucket", "register_library_preview_routes"]
