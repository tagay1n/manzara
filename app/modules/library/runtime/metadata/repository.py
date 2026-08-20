"""Database access helpers for metadata extraction and evaluation flows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import and_
from sqlalchemy import select

from models import Document, LibraryMetadataEvaluationState, Metadata
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
    model_pool: Sequence[str],
) -> list[tuple[Document, Metadata]]:
    """Return unevaluated or internally inconsistent metadata rows."""
    predicate = (
        (
            Metadata.lib.is_(None)
            | and_(
                Metadata.lib.is_(True),
                Metadata.classification_id.is_(None),
            )
            | and_(
                Metadata.lib.is_(False),
                Metadata.classification_id.is_not(None),
            )
        )
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
            select(Document, Metadata, LibraryMetadataEvaluationState)
            .join(Metadata, Metadata.md5 == Document.md5)
            .outerjoin(
                LibraryMetadataEvaluationState,
                LibraryMetadataEvaluationState.md5 == Document.md5,
            )
            .where(predicate)
        )
        rows = session.execute(stmt)
        return [
            (doc, meta)
            for doc, meta, state in rows
            if _evaluation_state_allows_retry(state, model_pool)
        ][: max(0, int(batch_size))]


def count_docs_for_evaluation(
    lang_codes: list[str],
    excluded_md5s: set[str],
    model_pool: Sequence[str],
) -> int:
    """Return total number of docs pending library applicability evaluation."""
    return len(
        fetch_docs_for_evaluation(
            batch_size=2**31 - 1,
            lang_codes=lang_codes,
            excluded_md5s=excluded_md5s,
            model_pool=model_pool,
        )
    )


def mark_docs_as_non_applicable(
    md5s: list[str], *, eval_method: str = "rules/v1"
) -> None:
    """Set metadata.lib=False for provided document ids."""
    if not md5s:
        return

    with get_session() as session:
        for md5 in md5s:
            row = session.get(Metadata, md5)
            if row is not None:
                row.lib = False
                row.lib_eval_method = eval_method
                row.classification_id = None
        session.commit()


def _evaluation_state_allows_retry(
    state: LibraryMetadataEvaluationState | None,
    model_pool: Sequence[str],
) -> bool:
    if state is None or str(state.status or "") != "terminal":
        return True
    previous = {
        str(model)
        for model in (state.model_pool_json or [])
        if str(model or "").strip()
    }
    current = {str(model) for model in model_pool if str(model or "").strip()}
    return previous != current


def get_evaluation_attempted_models(md5: str) -> set[str]:
    """Return content-level models already tried for one document."""
    with get_session() as session:
        state = session.get(LibraryMetadataEvaluationState, str(md5))
        attempts = state.attempts_json if state is not None else []
        return {
            str(item.get("model") or "")
            for item in (attempts or [])
            if isinstance(item, dict) and str(item.get("model") or "").strip()
        }


def record_evaluation_model_failure(
    md5: str,
    *,
    model_name: str,
    kind: str,
    error: str,
    models: Sequence[str],
    run_id: int | None,
) -> None:
    """Persist one evaluation response failure exactly once."""
    now = datetime.now(timezone.utc)
    attempt = {
        "model": str(model_name),
        "kind": str(kind),
        "error": str(error or "")[:4000],
        "recorded_at": now.isoformat(),
    }
    with get_session() as session:
        state = session.get(LibraryMetadataEvaluationState, str(md5))
        if state is None:
            state = LibraryMetadataEvaluationState(
                md5=str(md5),
                status="partial",
                attempts_json=[],
                model_pool_json=list(models),
                created_at=now,
                updated_at=now,
            )
            session.add(state)
        attempts = [
            dict(item) for item in (state.attempts_json or []) if isinstance(item, dict)
        ]
        if not any(str(item.get("model") or "") == str(model_name) for item in attempts):
            attempts.append(attempt)
        state.status = "partial"
        state.attempts_json = attempts
        state.model_pool_json = list(models)
        state.last_run_id = run_id
        state.terminal_reason = None
        state.updated_at = now
        session.commit()


def mark_evaluation_terminal(
    md5: str,
    *,
    models: Sequence[str],
    run_id: int | None,
    reason: str,
) -> None:
    """Defer a document until its configured model pool changes."""
    now = datetime.now(timezone.utc)
    with get_session() as session:
        state = session.get(LibraryMetadataEvaluationState, str(md5))
        if state is None:
            state = LibraryMetadataEvaluationState(
                md5=str(md5),
                attempts_json=[],
                created_at=now,
            )
            session.add(state)
        state.status = "terminal"
        state.model_pool_json = list(models)
        state.last_run_id = run_id
        state.terminal_reason = str(reason or "")[:4000]
        state.updated_at = now
        session.commit()


def clear_evaluation_state(md5: str) -> None:
    """Remove retry state after one valid evaluation is stored."""
    with get_session() as session:
        state = session.get(LibraryMetadataEvaluationState, str(md5))
        if state is not None:
            session.delete(state)
            session.commit()
