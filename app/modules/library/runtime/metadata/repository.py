"""Database access helpers for metadata extraction and evaluation flows."""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy import select

from models import Document, Metadata
from core.db import get_session


def fetch_docs_for_metadata_extraction(limit: int, excluded_md5s: set[str]) -> list[Document]:
    """Return a batch of docs that still need metadata extraction."""
    predicate = (
        Metadata.md5.is_(None)
        & (
            Document.content_url.is_not(None)
            | (Document.mime_type == "application/pdf")
        )
    )
    if excluded_md5s:
        predicate = predicate & Document.md5.not_in(excluded_md5s)

    with get_session() as session:
        stmt = (
            select(Document)
            .outerjoin(Metadata, Metadata.md5 == Document.md5)
            .where(predicate)
            .limit(limit)
        )
        return list(session.scalars(stmt))


def fetch_docs_for_evaluation(
    batch_size: int,
    lang_codes: list[str],
    excluded_md5s: set[str],
) -> list[tuple[Document, Metadata]]:
    """Return docs with metadata rows that are pending library applicability evaluation."""
    predicate = (
        Metadata.lib.is_(None)
        & Document.language.in_(lang_codes)
        & (
            Document.content_url.is_not(None)
            | (Document.mime_type == "application/pdf")
        )
    )
    if excluded_md5s:
        predicate = predicate & Document.md5.not_in(excluded_md5s)

    with get_session() as session:
        stmt = (
            select(Document, Metadata)
            .join(Metadata, Metadata.md5 == Document.md5)
            .where(predicate)
            .limit(batch_size)
        )
        return [(doc, meta) for doc, meta in session.execute(stmt)]


def count_docs_for_evaluation(
    lang_codes: list[str],
    excluded_md5s: set[str],
) -> int:
    """Return total number of docs pending library applicability evaluation."""
    predicate = (
        Metadata.lib.is_(None)
        & Document.language.in_(lang_codes)
        & (
            Document.content_url.is_not(None)
            | (Document.mime_type == "application/pdf")
        )
    )
    if excluded_md5s:
        predicate = predicate & Document.md5.not_in(excluded_md5s)

    with get_session() as session:
        stmt = (
            select(func.count())
            .select_from(Document)
            .join(Metadata, Metadata.md5 == Document.md5)
            .where(predicate)
        )
        return int(session.scalar(stmt) or 0)


def mark_docs_as_non_applicable(md5s: list[str]) -> None:
    """Set metadata.lib=False for provided document ids."""
    if not md5s:
        return

    with get_session() as session:
        for md5 in md5s:
            row = session.get(Metadata, md5)
            if row is not None:
                row.lib = False
        session.commit()
