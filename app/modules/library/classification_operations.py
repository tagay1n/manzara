"""Library classification detail operations."""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import text

from app.modules.library.classification_insights import _format_path, _serialize_value
from app.modules.library.response_envelope import available_payload, unavailable_payload
from app.modules.library.stats import create_runtime_engine, dispose_runtime_engine


def get_classification_detail(
    classification_id: int,
    *,
    docs_page: int = 1,
    docs_page_size: int = 40,
) -> Dict[str, Any]:
    """Return one classification with linked docs and language split."""
    try:
        classification_id = int(classification_id)
        docs_page = max(1, int(docs_page))
        docs_page_size = max(1, min(200, int(docs_page_size)))
        offset = (docs_page - 1) * docs_page_size

        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            classification = conn.execute(
                text(
                    """
                    SELECT c.id,c.ddc,c.path_en,c.path_tt,c.status,c.created_by,c.created_at,
                           COALESCE(u.usage_count,0) usage_count
                    FROM classification c
                    LEFT JOIN (
                        SELECT classification_id,COUNT(md5) usage_count
                        FROM metadata WHERE classification_id IS NOT NULL
                        GROUP BY classification_id
                    ) u ON u.classification_id=c.id
                    WHERE c.id=:classification_id
                    """
                ),
                {"classification_id": classification_id},
            ).mappings().first()
            if not classification:
                raise ValueError("Classification not found")

            docs_total = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM metadata WHERE classification_id=:classification_id"),
                    {"classification_id": classification_id},
                ).scalar()
                or 0
            )
            documents = conn.execute(
                text(
                    """
                    SELECT d.md5,d.language,d.ya_path,d.mime_type,d.document_url,d.content_url,
                           d.full,d.sharing_restricted,m.lib_eval_method
                    FROM metadata m JOIN document d ON d.md5=m.md5
                    WHERE m.classification_id=:classification_id
                    ORDER BY d.md5 LIMIT :limit OFFSET :offset
                    """
                ),
                {
                    "classification_id": classification_id,
                    "limit": docs_page_size,
                    "offset": offset,
                },
            ).mappings().all()
            languages = conn.execute(
                text(
                    """
                    SELECT COALESCE(d.language,'unknown') language,COUNT(*) count
                    FROM metadata m JOIN document d ON d.md5=m.md5
                    WHERE m.classification_id=:classification_id
                    GROUP BY COALESCE(d.language,'unknown')
                    ORDER BY COUNT(*) DESC,COALESCE(d.language,'unknown')
                    """
                ),
                {"classification_id": classification_id},
            ).mappings().all()
        dispose_runtime_engine(engine)

        return available_payload(
            config_source=config_source,
            classification={
                "classification_id": int(classification.get("id") or 0),
                "ddc": str(classification.get("ddc") or ""),
                "path": _format_path(classification.get("path_en")),
                "path_tt": _format_path(classification.get("path_tt")),
                "status": str(classification.get("status") or ""),
                "created_by": str(classification.get("created_by") or ""),
                "created_at": _serialize_value(classification.get("created_at")),
                "usage_count": int(classification.get("usage_count") or 0),
            },
            linked_docs={
                "page": docs_page,
                "page_size": docs_page_size,
                "total": docs_total,
                "total_pages": max(1, (docs_total + docs_page_size - 1) // docs_page_size),
                "items": [
                    {
                        "md5": str(doc.get("md5") or ""),
                        "language": str(doc.get("language") or ""),
                        "ya_path": str(doc.get("ya_path") or ""),
                        "mime_type": str(doc.get("mime_type") or ""),
                        "document_url": str(doc.get("document_url") or ""),
                        "content_url": str(doc.get("content_url") or ""),
                        "full": bool(doc.get("full")) if doc.get("full") is not None else None,
                        "sharing_restricted": bool(doc.get("sharing_restricted"))
                        if doc.get("sharing_restricted") is not None
                        else None,
                        "lib_eval_method": str(doc.get("lib_eval_method") or ""),
                    }
                    for doc in documents
                ],
            },
            language_distribution=[
                {"language": str(row.get("language") or ""), "count": int(row.get("count") or 0)}
                for row in languages
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return unavailable_payload(
            exc,
            classification=None,
            linked_docs={
                "page": 1,
                "page_size": docs_page_size,
                "total": 0,
                "total_pages": 1,
                "items": [],
            },
            language_distribution=[],
        )


__all__ = ["get_classification_detail"]
