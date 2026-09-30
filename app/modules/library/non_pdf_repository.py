"""PostgreSQL queue and checkpoints for rich non-PDF extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.postgres_engine import acquire_postgres_engine, release_postgres_engine

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_AUTOMATIC_ATTEMPTS = 3
_STATUSES = {"processing", "ready", "failed", "unsupported", "deferred"}


@dataclass(frozen=True)
class NonPdfCandidate:
    md5: str
    mime_type: str
    source_path: str
    document_url: str
    primary_storage_size: int
    content_url: str | None
    prior_detected_format: str | None = None


class NonPdfExtractionRepository:
    """Own candidate selection and compact extraction state."""

    def __init__(self, database_url: str, *, schema: str = "monocorpus") -> None:
        normalized = str(schema or "monocorpus").strip() or "monocorpus"
        if not _SCHEMA_RE.fullmatch(normalized):
            raise ValueError(f"Invalid database schema: {normalized!r}")
        self.engine: Engine = acquire_postgres_engine(
            str(database_url), schema=normalized
        )

    def dispose(self) -> None:
        release_postgres_engine(self.engine)

    def list_powerpoint_checkpoints(self, *, extractor_version: str) -> list[NonPdfCandidate]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT d.md5, d.mime_type, d.ya_path, d.document_url,
                           d.primary_storage_size, d.content_url,
                           state.detected_format AS prior_detected_format
                    FROM document d
                    JOIN library_non_pdf_extraction_state state ON state.md5=d.md5
                    WHERE state.extractor_version=:extractor_version
                      AND state.detected_format='powerpoint'
                    ORDER BY d.md5
                    """
                ),
                {"extractor_version": extractor_version},
            ).mappings().all()
        return [self._candidate(row) for row in rows]

    def backfill_verified_sources(
        self,
        items: list[tuple[NonPdfCandidate, str, str]],
        *,
        previous_version: str,
    ) -> None:
        """Record independently verified OLE roots without republishing content."""
        allowed = {
            "doc": "application/msword",
            "powerpoint": "application/vnd.ms-powerpoint",
        }
        with self.engine.begin() as conn:
            for candidate, detected_format, verified_mime in items:
                if allowed.get(detected_format) != verified_mime:
                    raise ValueError(f"Unverified OLE MIME for {candidate.md5}")
                state = conn.execute(
                    text(
                        """
                        UPDATE library_non_pdf_extraction_state
                        SET detected_format=:detected_format,
                            verified_source_mime=:verified_mime,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE md5=:md5 AND extractor_version=:previous_version
                          AND detected_format='powerpoint'
                        """
                    ),
                    {
                        "md5": candidate.md5,
                        "detected_format": detected_format,
                        "verified_mime": verified_mime,
                        "previous_version": previous_version,
                    },
                )
                if int(state.rowcount or 0) != 1:
                    raise RuntimeError(f"OLE checkpoint changed for {candidate.md5}")
                document = conn.execute(
                    text(
                        """
                        UPDATE document SET mime_type=:verified_mime
                        WHERE md5=:md5
                          AND document_url IS NOT DISTINCT FROM :document_url
                          AND primary_storage_size IS NOT DISTINCT FROM :primary_storage_size
                          AND LOWER(BTRIM(COALESCE(mime_type, '')))
                              = LOWER(BTRIM(:previous_mime))
                        """
                    ),
                    {
                        "md5": candidate.md5,
                        "document_url": candidate.document_url,
                        "primary_storage_size": candidate.primary_storage_size,
                        "previous_mime": candidate.mime_type,
                        "verified_mime": verified_mime,
                    },
                )
                if int(document.rowcount or 0) != 1:
                    raise RuntimeError(f"OLE document changed for {candidate.md5}")

    def list_candidates(
        self,
        *,
        extractor_version: str,
        powerpoint_version: str | None = None,
        spreadsheet_version: str | None = None,
        limit: int | None = None,
        per_mime_limit: int | None = None,
        retry_known_failures: bool = False,
        only_md5s: frozenset[str] | None = None,
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
                  AND NOT EXISTS (
                      SELECT 1
                      FROM document_cleanup_queue cleanup
                      WHERE cleanup.scope = 'document'
                        AND cleanup.md5 = d.md5
                        AND cleanup.reason = 'corrupted'
                        AND cleanup.status IN ('planned', 'running', 'failed')
                  )
            )
            SELECT d.md5, d.mime_type, d.ya_path, d.document_url,
                   d.primary_storage_size, d.content_url,
                   state.detected_format AS prior_detected_format
            FROM eligible d
            LEFT JOIN library_non_pdf_extraction_state state ON state.md5 = d.md5
            WHERE (:per_mime_limit IS NULL OR d.mime_rank <= :per_mime_limit)
              AND (
                    state.md5 IS NULL
                    OR state.extractor_version IS DISTINCT FROM
                        CASE state.detected_format
                            WHEN 'powerpoint' THEN :powerpoint_version
                            WHEN 'spreadsheet' THEN :spreadsheet_version
                            ELSE :extractor_version END
                    OR state.status = 'processing'
                    OR (
                        state.status = 'failed'
                        AND (
                            :retry_known_failures
                            OR state.attempt_count < :max_automatic_attempts
                        )
                    )
                    OR (
                        :retry_known_failures
                        AND state.status = 'deferred'
                    )
                  )
            ORDER BY
                CASE
                    WHEN state.md5 IS NULL THEN 0
                    WHEN state.extractor_version IS DISTINCT FROM
                        CASE state.detected_format
                            WHEN 'powerpoint' THEN :powerpoint_version
                            WHEN 'spreadsheet' THEN :spreadsheet_version
                            ELSE :extractor_version END
                        THEN 1
                    WHEN state.status = 'processing' THEN 2
                    WHEN state.status = 'failed' THEN 3
                    WHEN state.status = 'deferred' THEN 4
                    ELSE 5
                END,
                CASE WHEN d.content_url IS NULL THEN 0 ELSE 1 END,
                d.mime_key, d.mime_rank, d.md5
        """
        if only_md5s is not None:
            sql = sql.replace(
                "            ORDER BY\n",
                "              AND d.md5 = ANY(:only_md5s)\n            ORDER BY\n",
                1,
            )
        normalized_per_mime = (
            None if per_mime_limit is None else max(0, int(per_mime_limit))
        )
        params: dict[str, Any] = {
            "extractor_version": str(extractor_version),
            "powerpoint_version": str(powerpoint_version or extractor_version),
            "spreadsheet_version": str(spreadsheet_version or extractor_version),
            "per_mime_limit": normalized_per_mime,
            "max_automatic_attempts": MAX_AUTOMATIC_ATTEMPTS,
            "retry_known_failures": bool(retry_known_failures),
        }
        if only_md5s is not None:
            params["only_md5s"] = sorted(only_md5s)
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

    def record_detected_source(
        self,
        candidate: NonPdfCandidate,
        *,
        detected_format: str,
        extractor_version: str,
        verified_mime_type: str | None = None,
    ) -> None:
        """Checkpoint byte detection and correct a stale catalog MIME atomically."""
        if verified_mime_type is not None and {
            "doc": "application/msword",
            "powerpoint": "application/vnd.ms-powerpoint",
        }.get(detected_format) != verified_mime_type:
            raise ValueError(f"Unverified OLE MIME for {candidate.md5}")
        with self.engine.begin() as conn:
            state = conn.execute(
                text(
                    """
                    UPDATE library_non_pdf_extraction_state
                    SET detected_format=:detected_format,
                        verified_source_mime=:verified_mime_type,
                        attempt_count=CASE
                            WHEN extractor_version IS DISTINCT FROM :extractor_version
                            THEN 1 ELSE attempt_count END,
                        extractor_version=:extractor_version,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE md5=:md5 AND status='processing'
                    """
                ),
                {
                    "md5": candidate.md5,
                    "detected_format": str(detected_format),
                    "extractor_version": str(extractor_version),
                    "verified_mime_type": verified_mime_type,
                },
            )
            if int(state.rowcount or 0) != 1:
                raise RuntimeError(f"Extraction attempt changed for {candidate.md5}")
            corrected_mime = verified_mime_type
            if corrected_mime is None:
                return
            updated = conn.execute(
                text(
                    """
                    UPDATE document SET mime_type=:mime_type
                    WHERE md5=:md5
                      AND document_url IS NOT DISTINCT FROM :document_url
                      AND primary_storage_size IS NOT DISTINCT FROM :primary_storage_size
                      AND LOWER(BTRIM(COALESCE(mime_type, '')))
                          = LOWER(BTRIM(:catalog_mime_type))
                    """
                ),
                {
                    "md5": candidate.md5,
                    "document_url": candidate.document_url,
                    "primary_storage_size": candidate.primary_storage_size,
                    "catalog_mime_type": candidate.mime_type,
                    "mime_type": corrected_mime,
                },
            )
            if int(updated.rowcount or 0) != 1:
                raise RuntimeError(
                    f"Document source changed before MIME correction for {candidate.md5}"
                )

    def start_attempt(self, md5: str, *, extractor_version: str, run_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO library_non_pdf_extraction_state (
                        md5, extractor_version, status, attempt_count, last_run_id,
                        created_at, updated_at
                    ) VALUES (
                        :md5, :extractor_version, 'processing', 1, NULL,
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
                        attempt_count = CASE
                            WHEN library_non_pdf_extraction_state.extractor_version
                                 IS DISTINCT FROM EXCLUDED.extractor_version
                            THEN 1
                            ELSE library_non_pdf_extraction_state.attempt_count + 1
                        END,
                        last_run_id = NULL,
                        error_text = NULL,
                        generated_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "md5": str(md5),
                    "extractor_version": str(extractor_version),
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
                        last_run_id=NULL,
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
                        status='ready', last_run_id=NULL, error_text=NULL,
                        generated_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE md5=:md5
                    """
                ),
                {
                    "md5": candidate.md5,
                    "extractor_version": str(extractor_version),
                    "detected_format": str(detected_format),
                },
            )
            if int(state.rowcount or 0) != 1:
                raise LookupError(
                    f"Extraction state was not started for {candidate.md5}"
                )
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
            prior_detected_format=(
                str(row.get("prior_detected_format") or "").strip() or None
            ),
        )


__all__ = [
    "MAX_AUTOMATIC_ATTEMPTS",
    "NonPdfCandidate",
    "NonPdfExtractionRepository",
]
