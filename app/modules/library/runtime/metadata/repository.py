"""Database access helpers for metadata extraction and evaluation flows."""

from __future__ import annotations

from functools import lru_cache
from typing import Sequence

from core.db import get_session
from models import (
    Document,
    LibraryUpstreamMetadata,
    Metadata,
)
from sqlalchemy import and_, select

from app.local_state import AIItemCheckpointStore
from app.settings import _load_local_state_path

EVALUATION_PROMPT_VERSION = "prompt.v3"
_FLOW_ID = "library.metadata_evaluate"


@lru_cache(maxsize=4)
def _checkpoint_store(path: str) -> AIItemCheckpointStore:
    return AIItemCheckpointStore(path)


def _checkpoints() -> AIItemCheckpointStore:
    return _checkpoint_store(str(_load_local_state_path()))


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
) -> list[tuple[Document, Metadata, dict | None]]:
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
            select(
                Document,
                Metadata,
                LibraryUpstreamMetadata.payload_json,
            )
            .join(Metadata, Metadata.md5 == Document.md5)
            .outerjoin(
                LibraryUpstreamMetadata,
                LibraryUpstreamMetadata.md5 == Document.md5,
            )
            .where(predicate)
        )
        rows = list(session.execute(stmt))
        states = _checkpoints().get_many(
            _FLOW_ID, [str(doc.md5) for doc, _meta, _upstream in rows]
        )
        return [
            (doc, meta, upstream_metadata)
            for doc, meta, upstream_metadata in rows
            if _evaluation_state_allows_retry(
                states.get(str(doc.md5)), model_pool
            )
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
    state: dict | None, model_pool: Sequence[str]
) -> bool:
    if state is None:
        return True
    if state.get("contract_version") != EVALUATION_PROMPT_VERSION:
        return True
    if str(state.get("status") or "") != "terminal":
        return True
    previous = {
        str(model)
        for model in (state.get("model_pool") or [])
        if str(model or "").strip()
    }
    current = {str(model) for model in model_pool if str(model or "").strip()}
    return previous != current


def get_evaluation_attempted_models(md5: str) -> set[str]:
    """Return content-level models already tried for one document."""
    state = _checkpoints().get(_FLOW_ID, str(md5))
    attempts = (
        state.get("attempts") or []
        if state and state.get("contract_version") == EVALUATION_PROMPT_VERSION
        else []
    )
    return {
        str(item.get("model") or "")
        for item in attempts
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
    _checkpoints().record_failure(
        flow_id=_FLOW_ID, item_id=str(md5),
        contract_version=EVALUATION_PROMPT_VERSION,
        model_name=str(model_name), kind=str(kind), error=str(error),
        models=models, run_id=run_id,
    )


def mark_evaluation_terminal(
    md5: str,
    *,
    models: Sequence[str],
    run_id: int | None,
    reason: str,
) -> None:
    """Defer a document until its configured model pool changes."""
    _checkpoints().mark_terminal(
        flow_id=_FLOW_ID, item_id=str(md5),
        contract_version=EVALUATION_PROMPT_VERSION,
        models=models, reason=str(reason), run_id=run_id,
    )


def clear_evaluation_state(md5: str) -> None:
    """Remove retry state after one valid evaluation is stored."""
    _checkpoints().clear(_FLOW_ID, str(md5))
