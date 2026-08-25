"""PostgreSQL queue and checkpoints for rich non-PDF extraction."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STATUSES = {"processing", "ready", "failed", "unsupported"}


@dataclass(frozen=True)
class NonPdfCandidate:
    md5: str
    mime_type: str
    source_path: str
    document_url: str
    primary_storage_size: int
    content_url: str | None


class NonPdfExtractionRepository:
    """Own candidate selection and compact extraction state."""

    def __init__(self, database_url: str, *, schema: str = "monocorpus") -> None:
        normalized = str(schema or "monocorpus").strip() or "monocorpus"
        if not _SCHEMA_RE.fullmatch(normalized):
            raise ValueError(f"Invalid database schema: {normalized!r}")
        self.engine: Engine = create_engine(
            str(database_url),
            connect_args={"options": f"-csearch_path={normalized},public"},
        )

    def dispose(self) -> None:
        self.engine.dispose()

    def list_candidates(
        self,
        *,
        extractor_version: str,
        limit: int | None = None,
        per_mime_limit: int | None = None,
    ) -> list[NonPdfCandidate]:
        sql = """
            WITH eligible AS (
                SELECT d.md5, d.mime_type, d.ya_path, d.document_url,
                       d.primary_storage_size, d.content_url,
                       COALESCE(NULLIF(LOWER(BTRIM(d.mime_type)), ''), 'unknown')
                           AS mime_key,
                       ROW_NUMBER() OVER (
                           PARTITION BY COALESCE(
                               NULLIF(LOWER(BTRIM(d.mime_type)), ''), 'unknown'
                           )
                           ORDER BY d.md5
                       ) AS mime_rank
                FROM document d
                WHERE d.document_url IS NOT NULL
                  AND d.primary_storage_size IS NOT NULL
                  AND d.primary_storage_verified_at IS NOT NULL
                  AND LOWER(BTRIM(COALESCE(d.mime_type, '')))
                      <> 'application/pdf'
            )
            SELECT d.md5, d.mime_type, d.ya_path, d.document_url,
                   d.primary_storage_size, d.content_url
            FROM eligible d
            LEFT JOIN library_non_pdf_extraction_state state ON state.md5 = d.md5
            WHERE (:per_mime_limit IS NULL OR d.mime_rank <= :per_mime_limit)
              AND (
                    state.md5 IS NULL
                    OR state.extractor_version IS DISTINCT FROM :extractor_version
                    OR state.status NOT IN ('ready', 'unsupported')
                  )
            ORDER BY
                CASE WHEN d.content_url IS NULL THEN 0 ELSE 1 END,
                d.mime_key, d.mime_rank, d.md5
        """
        normalized_per_mime = (
            None if per_mime_limit is None else max(0, int(per_mime_limit))
        )
        params: dict[str, Any] = {
            "extractor_version": str(extractor_version),
            "per_mime_limit": normalized_per_mime,
        }
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = max(0, int(limit))
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        candidates: list[NonPdfCandidate] = []
        seen: set[str] = set()
        for row in rows:
            candidate = self._candidate(row)
            if not candidate.md5:
                raise RuntimeError("Non-PDF extraction candidate has no MD5")
            if candidate.md5 in seen:
                raise RuntimeError(
                    f"Duplicate document MD5 {candidate.md5}; refusing extraction"
                )
            seen.add(candidate.md5)
            candidates.append(candidate)
        return candidates

    def start_attempt(
        self, md5: str, *, extractor_version: str, run_id: int
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO library_non_pdf_extraction_state (
                        md5, extractor_version, status, attempt_count, last_run_id,
                        created_at, updated_at
                    ) VALUES (
                        :md5, :extractor_version, 'processing', 1, :run_id,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (md5) DO UPDATE SET
                        extractor_version = EXCLUDED.extractor_version,
                        detected_format = CASE
                            WHEN library_non_pdf_extraction_state.extractor_version
                                 = EXCLUDED.extractor_version
                            THEN library_non_pdf_extraction_state.detected_format
                            ELSE NULL
                        END,
                        status = 'processing',
                        attempt_count = library_non_pdf_extraction_state.attempt_count + 1,
                        last_run_id = EXCLUDED.last_run_id,
                        error_text = NULL,
                        generated_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "md5": str(md5),
                    "extractor_version": str(extractor_version),
                    "run_id": int(run_id),
                },
            )

    def mark_outcome(
        self,
        md5: str,
        *,
        extractor_version: str,
        detected_format: str | None,
        status: str,
        run_id: int,
        error_text: str | None = None,
    ) -> None:
        normalized = str(status or "").strip()
        if normalized not in _STATUSES:
            raise ValueError(f"Invalid non-PDF extraction status: {status!r}")
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE library_non_pdf_extraction_state
                    SET extractor_version=:extractor_version,
                        detected_format=:detected_format,
                        status=:status,
                        last_run_id=:run_id,
                        error_text=:error_text,
                        generated_at=CASE WHEN :status='ready'
                                          THEN CURRENT_TIMESTAMP ELSE NULL END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE md5=:md5
                    """
                ),
                {
                    "md5": str(md5),
                    "extractor_version": str(extractor_version),
                    "detected_format": str(detected_format or "").strip() or None,
                    "status": normalized,
                    "run_id": int(run_id),
                    "error_text": str(error_text or "").strip()[:4000] or None,
                },
            )
            if int(result.rowcount or 0) != 1:
                raise LookupError(f"Extraction state was not started for {md5}")

    def save_success(
        self,
        candidate: NonPdfCandidate,
        *,
        extractor_version: str,
        detected_format: str,
        run_id: int,
        content_url: str,
    ) -> bool:
        """Atomically publish content only while the source snapshot is unchanged."""
        with self.engine.begin() as conn:
            updated = conn.execute(
                text(
                    """
                    UPDATE document
                    SET content_url=:content_url,
                        content_extraction_method=:extractor_version
                    WHERE md5=:md5
                      AND document_url IS NOT DISTINCT FROM :document_url
                      AND primary_storage_size IS NOT DISTINCT FROM :primary_storage_size
                      AND content_url IS NOT DISTINCT FROM :previous_content_url
                    """
                ),
                {
                    "md5": candidate.md5,
                    "content_url": str(content_url),
                    "extractor_version": str(extractor_version),
                    "document_url": candidate.document_url,
                    "primary_storage_size": candidate.primary_storage_size,
                    "previous_content_url": candidate.content_url,
                },
            )
            if int(updated.rowcount or 0) != 1:
                return False
            state = conn.execute(
                text(
                    """
                    UPDATE library_non_pdf_extraction_state
                    SET extractor_version=:extractor_version,
                        detected_format=:detected_format,
                        status='ready', last_run_id=:run_id, error_text=NULL,
                        generated_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE md5=:md5
                    """
                ),
                {
                    "md5": candidate.md5,
                    "extractor_version": str(extractor_version),
                    "detected_format": str(detected_format),
                    "run_id": int(run_id),
                },
            )
            if int(state.rowcount or 0) != 1:
                raise LookupError(f"Extraction state was not started for {candidate.md5}")
        return True

    @staticmethod
    def _candidate(row: Mapping[str, Any]) -> NonPdfCandidate:
        return NonPdfCandidate(
            md5=str(row.get("md5") or "").strip().lower(),
            mime_type=str(row.get("mime_type") or "").strip().lower(),
            source_path=str(row.get("ya_path") or ""),
            document_url=str(row.get("document_url") or "").strip(),
            primary_storage_size=int(row.get("primary_storage_size") or 0),
            content_url=str(row.get("content_url") or "").strip() or None,
        )


__all__ = ["NonPdfCandidate", "NonPdfExtractionRepository"]
