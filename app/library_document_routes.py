"""Stable browser routes for opening Library source documents."""

from __future__ import annotations

import re
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from app.modules.library.document_access import resolve_document_open_url


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


__all__ = ["register_library_document_routes"]
