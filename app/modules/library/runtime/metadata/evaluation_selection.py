"""Catalog selection, deterministic early skips, and known classifications."""

from __future__ import annotations

import re
from typing import Any, Iterable

from sqlalchemy import func, select

from app.gemini_workers import emit_gemini_worker_log
from app.modules.library.runtime.metadata.fields import extract_flat_fields
from app.modules.library.runtime.metadata.repository import (
    count_docs_for_evaluation,
    fetch_docs_for_evaluation,
    mark_docs_as_non_applicable,
)
from app.modules.library.runtime.models import Classification, Metadata
from app.modules.runtime_shared_utils import get_session

from .evaluation_channel import Channel
from .evaluation_classification import _normalize_classification_path, _normalize_ddc
from .evaluation_types import EvaluationTask

LEGAL_DOC_PATTERNS = [
    re.compile(r"^(?=.*common_crawl)(?=.*npa_ta_).*\.pdf$"),
    re.compile(r"^(?=.*pdf законов с pravo\.gov).*\.pdf$"),
]


DEFAULT_KNOWN_CLASSIFICATIONS_LIMIT = 500


def _load_batch(
    config: dict,
    batch_size: int,
    channel: "Channel",
    models: list[str],
) -> list[EvaluationTask]:
    lang_codes = config["sup_langs"]["tt"]["codes"]
    rows = fetch_docs_for_evaluation(
        batch_size=batch_size,
        lang_codes=lang_codes,
        excluded_md5s=channel.get_deferred_docs(),
        model_pool=models,
    )
    return [
        EvaluationTask(
            md5=doc.md5,
            ya_path=doc.ya_path,
            language=doc.language,
            page_count=extract_flat_fields(meta.schema_org if meta else None).get(
                "page_count"
            ),
            full=doc.full,
            sharing_restricted=doc.sharing_restricted,
            ya_public_url=doc.ya_public_url,
            mime_type=doc.mime_type,
            document_url=doc.document_url,
            upstream_metadata=(
                dict(upstream_metadata) if isinstance(upstream_metadata, dict) else None
            ),
            content_url=doc.content_url,
            schema_org=meta.schema_org if meta else None,
        )
        for doc, meta, upstream_metadata in rows
    ]


def _count_remaining(
    config: dict,
    channel: "Channel",
    models: list[str],
) -> int:
    """Count docs still pending evaluation for current language filters."""
    lang_codes = config["sup_langs"]["tt"]["codes"]
    return count_docs_for_evaluation(
        lang_codes=lang_codes,
        excluded_md5s=set(),
        model_pool=models,
    )


def _early_skip(
    docs: Iterable[EvaluationTask],
) -> tuple[list[EvaluationTask], list[tuple[str, str]]]:
    probables = []
    non_applicables = []
    for doc in docs:
        if doc.full is not True:
            non_applicables.append((doc.md5, "not full"))
            continue
        if doc.sharing_restricted is True:
            non_applicables.append((doc.md5, "sharing restricted"))
            continue
        if doc.ya_path and any(
            pattern.match(doc.ya_path) for pattern in LEGAL_DOC_PATTERNS
        ):
            non_applicables.append((doc.md5, "legal doc"))
            continue
        probables.append(doc)
    return probables, non_applicables


def _save_non_applicable(non_applicables: list[tuple[str, str]]) -> None:
    if not non_applicables:
        return
    emit_gemini_worker_log(
        f"Marking {len(non_applicables)} documents as non-applicable",
        worker_id="coordinator",
    )
    mark_docs_as_non_applicable([md5 for md5, _reason in non_applicables])


def _load_known_classifications(
    limit: int = DEFAULT_KNOWN_CLASSIFICATIONS_LIMIT,
) -> list[dict[str, Any]]:
    """Load top-N known classifications ordered by current usage frequency."""
    if limit <= 0:
        return []

    usage_subquery = (
        select(
            Metadata.classification_id.label("classification_id"),
            func.count(Metadata.md5).label("usage_count"),
        )
        .where(Metadata.classification_id.is_not(None))
        .group_by(Metadata.classification_id)
        .subquery()
    )

    known: list[dict[str, Any]] = []
    with get_session() as session:
        stmt = (
            select(Classification)
            .outerjoin(
                usage_subquery,
                usage_subquery.c.classification_id == Classification.id,
            )
            .order_by(
                func.coalesce(usage_subquery.c.usage_count, 0).desc(),
                Classification.ddc.asc(),
                Classification.id.asc(),
            )
            .limit(limit)
        )
        rows = session.scalars(stmt).all()
        for row in rows:
            path = _normalize_classification_path(row.path_en)
            ddc = _normalize_ddc(row.ddc)
            if not path or not ddc:
                continue
            known.append({"id": row.id, "ddc": ddc, "path": path})
    return known
