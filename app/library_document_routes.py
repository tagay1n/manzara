"""Stable browser routes for opening Library source documents."""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from botocore.exceptions import BotoCoreError, ClientError

from app.modules.library.document_access import (
    cache_document_for_local_open,
    resolve_document_open_url,
    resolve_local_cached_document,
)


_MD5_RE = re.compile(r"^[0-9a-f]{32}$")


def register_library_document_routes(
    app: FastAPI,
    *,
    state_provider: Callable[[], Any],
) -> None:
    """Register document redirects without exposing storage credentials."""

    @app.get("/api/library/documents/{md5}/open")
    def open_library_document(md5: str) -> RedirectResponse:
        digest = str(md5 or "").strip().lower()
        if not _MD5_RE.fullmatch(digest):
            raise HTTPException(
                status_code=400,
                detail="md5 must be a 32-character hexadecimal digest",
            )
        try:
            target = resolve_document_open_url(state_provider(), digest)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not target:
            raise HTTPException(status_code=404, detail="Document storage object not found")
        return RedirectResponse(target, status_code=307)

    @app.post("/api/library/documents/{md5}/cache")
    def cache_library_document(md5: str) -> JSONResponse:
        digest = str(md5 or "").strip().lower()
        if not _MD5_RE.fullmatch(digest):
            raise HTTPException(
                status_code=400,
                detail="md5 must be a 32-character hexadecimal digest",
            )
        try:
            cached = cache_document_for_local_open(state_provider(), digest)
        except (BotoCoreError, ClientError, OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Document could not be cached from primary storage: {exc}",
            ) from exc
        if cached is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return JSONResponse(
            {
                "md5": digest,
                "status": "ready",
                "open_url": (
                    f"/api/library/documents/{digest}/local/"
                    f"{quote(cached.source_name, safe='')}"
                ),
            }
        )

    def local_library_document_response(md5: str) -> FileResponse:
        digest = str(md5 or "").strip().lower()
        if not _MD5_RE.fullmatch(digest):
            raise HTTPException(
                status_code=400,
                detail="md5 must be a 32-character hexadecimal digest",
            )
        try:
            cached = resolve_local_cached_document(state_provider(), digest)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if cached is None:
            raise HTTPException(status_code=404, detail="Document is not available locally")
        return FileResponse(
            cached.path,
            media_type=cached.mime_type,
            filename=cached.source_name,
            content_disposition_type="inline",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/library/documents/{md5}/local/{filename}")
    def open_named_local_library_document(md5: str, filename: str) -> FileResponse:
        del filename
        return local_library_document_response(md5)

    @app.get("/api/library/documents/{md5}/local")
    def open_local_library_document(md5: str) -> FileResponse:
        return local_library_document_response(md5)


__all__ = ["register_library_document_routes"]
