"""Durable extraction results and verified facts, with local retry ownership."""

from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.operational_state import configured_store
from app.postgres_engine import acquire_postgres_engine, release_postgres_engine

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_AUTOMATIC_ATTEMPTS = 3
NON_PDF_SCOPE = "library.non_pdf_extraction"
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
        self.runtime = configured_store()
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
            conn.execute(text("SET TRANSACTION READ WRITE"))
            for candidate, detected_format, verified_mime in items:
                if allowed.get(detected_format) != verified_mime:
                    raise ValueError(f"Unverified OLE MIME for {candidate.md5}")
                state = conn.execute(
                    text(
                        """
                        UPDATE library_non_pdf_extraction_state
                        SET detected_format=:detected_format,
                            verified_source_mime=:verified_mime
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

    def list_candidates(self, *, extractor_version, powerpoint_version=None, spreadsheet_version=None,
                        odt_version=None, mobi_version=None, html_version=None, limit=None,
                        per_mime_limit=None, retry_known_failures=False, only_md5s=None):
        if not isinstance(retry_known_failures, bool):
            raise ValueError("retry_known_failures must be a boolean")
        for name, value in (("limit", limit), ("per_mime_limit", per_mime_limit)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer")
        if limit == 0:
            return []
        versions = {"powerpoint": powerpoint_version, "spreadsheet": spreadsheet_version,
                    "odt": odt_version, "mobi": mobi_version, "html": html_version}
        with self.engine.connect() as conn:
            rows = conn.execute(text("""SELECT d.*, s.extractor_version,s.detected_format,
                s.status AS durable_status,s.generated_at,s.updated_at AS durable_updated_at
                FROM document d LEFT JOIN library_non_pdf_extraction_state s USING(md5)
                WHERE d.document_url IS NOT NULL AND d.primary_storage_size IS NOT NULL
                AND d.primary_storage_verified_at IS NOT NULL
                AND lower(btrim(coalesce(d.mime_type,'')))<>'application/pdf'
                AND NOT EXISTS(SELECT 1 FROM document_cleanup_queue q WHERE q.md5=d.md5
                    AND q.scope='document' AND q.reason='corrupted' AND q.status IN ('planned','running','failed'))
                ORDER BY d.md5""")).mappings().all()
        runtime = self.runtime.list(NON_PDF_SCOPE)
        ranks = {}
        candidates = []
        for source in rows:
            row = dict(source)
            mime = str(row.get("mime_type") or "unknown").strip().lower()
            ranks[mime] = ranks.get(mime, 0) + 1
            if per_mime_limit is not None and ranks[mime] > per_mime_limit:
                continue
            if only_md5s is not None and row["md5"] not in only_md5s:
                continue
            local = runtime.get(row["md5"]) or {}
            local_time = local.get("updated_at")
            durable_time = row.get("durable_updated_at")
            use_local = bool(local_time and (durable_time is None or
                datetime.fromisoformat(local_time) >= durable_time)) and local.get("status") != "completed"
            state = local if use_local else {"status": row["durable_status"],
                "extractor_version": row["extractor_version"], "detected_format": row["detected_format"]}
            version = versions.get(state.get("detected_format")) or extractor_version
            status = state.get("status")
            changed = state.get("extractor_version") != version
            eligible = status is None or changed or status in {"processing", "detected"} or (
                status == "failed" and (retry_known_failures or int(local.get("attempt_count", 0)) < MAX_AUTOMATIC_ATTEMPTS)
            ) or (retry_known_failures and status == "deferred")
            if not eligible:
                continue
            row["prior_detected_format"] = state.get("detected_format")
            priority = 0 if status is None else 1 if changed else 2 if status in {"processing", "detected"} else 3 if status == "failed" else 4
            candidates.append((priority, row["content_url"] is not None, mime, ranks[mime], row["md5"], self._candidate(row)))
        candidates.sort(key=lambda item: item[:-1])
        return [item[-1] for item in candidates[:limit]]

    def record_detected_source(self, candidate, *, detected_format, extractor_version, verified_mime_type=None):
        if verified_mime_type is not None and {
            "doc": "application/msword", "powerpoint": "application/vnd.ms-powerpoint",
        }.get(detected_format) != verified_mime_type:
            raise ValueError(f"Unverified OLE MIME for {candidate.md5}")
        local = self.runtime.get(NON_PDF_SCOPE, candidate.md5)
        if not local or local["status"] != "processing":
            raise RuntimeError(f"Extraction attempt changed for {candidate.md5}")
        if verified_mime_type is not None:
            with self.engine.begin() as conn:
                conn.execute(text("SET TRANSACTION READ WRITE"))
                updated = conn.execute(text("""UPDATE document SET mime_type=:mime
                    WHERE md5=:md5 AND document_url IS NOT DISTINCT FROM :url
                    AND primary_storage_size IS NOT DISTINCT FROM :size
                    AND lower(btrim(coalesce(mime_type,'')))=lower(btrim(:old_mime))"""),
                    {"mime": verified_mime_type, "md5": candidate.md5, "url": candidate.document_url,
                     "size": candidate.primary_storage_size, "old_mime": candidate.mime_type})
                if updated.rowcount != 1:
                    raise RuntimeError(f"Document source changed before MIME correction for {candidate.md5}")
                conn.execute(text("""INSERT INTO library_non_pdf_extraction_state
                    (md5,extractor_version,detected_format,status,verified_source_mime,created_at,updated_at)
                    VALUES(:md5,:version,:format,'detected',:mime,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                    ON CONFLICT(md5) DO UPDATE SET detected_format=excluded.detected_format,
                    verified_source_mime=excluded.verified_source_mime,updated_at=CURRENT_TIMESTAMP"""),
                    {"md5": candidate.md5, "version": extractor_version, "format": detected_format, "mime": verified_mime_type})
        if local["extractor_version"] != extractor_version:
            local["attempt_count"] = 1
        local.update(extractor_version=extractor_version, detected_format=detected_format,
                     updated_at=datetime.now(timezone.utc).isoformat())
        self.runtime.put(NON_PDF_SCOPE, candidate.md5, local)

    def start_attempt(self, md5, *, extractor_version, run_id):
        with self.runtime.transaction() as conn:
            row = conn.execute("SELECT payload_json FROM operational_items WHERE scope=? AND item_id=?",
                               (NON_PDF_SCOPE, md5)).fetchone()
            previous = json.loads(row["payload_json"]) if row else {}
            same = previous.get("extractor_version") == extractor_version
            self.runtime.put(NON_PDF_SCOPE, md5, {
                "extractor_version": extractor_version, "status": "processing",
                "attempt_count": int(previous.get("attempt_count", 0)) + 1 if same else 1,
                "detected_format": previous.get("detected_format") if same else None,
                "last_run_id": run_id, "error_text": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, conn=conn)

    def mark_outcome(self, md5, *, extractor_version, detected_format, status, run_id, error_text=None):
        if status not in _STATUSES:
            raise ValueError(f"Invalid non-PDF extraction status: {status!r}")
        local = self.runtime.get(NON_PDF_SCOPE, md5)
        if local is None:
            raise LookupError(f"Extraction state was not started for {md5}")
        if status == "unsupported":
            with self.engine.begin() as conn:
                conn.execute(text("SET TRANSACTION READ WRITE"))
                conn.execute(text("""INSERT INTO library_non_pdf_extraction_state
                    (md5,extractor_version,detected_format,status,decision_reason,created_at,updated_at)
                    VALUES(:md5,:version,:format,'unsupported',:reason,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                    ON CONFLICT(md5) DO UPDATE SET extractor_version=excluded.extractor_version,
                    detected_format=excluded.detected_format,status='unsupported',decision_reason=excluded.decision_reason,
                    updated_at=CURRENT_TIMESTAMP"""),
                    {"md5": md5, "version": extractor_version, "format": detected_format, "reason": error_text})
        local.update(extractor_version=extractor_version, detected_format=detected_format,
            status="completed" if status in {"unsupported", "ready"} else status,
            last_run_id=run_id, error_text=error_text if status not in {"unsupported", "ready"} else None,
            updated_at=datetime.now(timezone.utc).isoformat())
        self.runtime.put(NON_PDF_SCOPE, md5, local)

    def save_success(self, candidate, *, extractor_version, detected_format, run_id, content_url):
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            updated = conn.execute(text("""UPDATE document SET content_url=:content_url,
                content_extraction_method=:version WHERE md5=:md5
                AND document_url IS NOT DISTINCT FROM :source_url
                AND primary_storage_size IS NOT DISTINCT FROM :size
                AND content_url IS NOT DISTINCT FROM :previous_url"""),
                {"content_url": content_url, "version": extractor_version, "md5": candidate.md5,
                 "source_url": candidate.document_url, "size": candidate.primary_storage_size,
                 "previous_url": candidate.content_url})
            if updated.rowcount != 1:
                return False
            conn.execute(text("""INSERT INTO library_non_pdf_extraction_state
                (md5,extractor_version,detected_format,status,created_at,updated_at,generated_at)
                VALUES(:md5,:version,:format,'ready',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                ON CONFLICT(md5) DO UPDATE SET extractor_version=excluded.extractor_version,
                detected_format=excluded.detected_format,status='ready',decision_reason=NULL,
                updated_at=CURRENT_TIMESTAMP,generated_at=CURRENT_TIMESTAMP"""),
                {"md5": candidate.md5, "version": extractor_version, "format": detected_format})
        self.mark_outcome(candidate.md5, extractor_version=extractor_version, detected_format=detected_format,
                          status="ready", run_id=run_id)
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
