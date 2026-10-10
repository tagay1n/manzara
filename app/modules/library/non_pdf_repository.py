"""Durable extraction results and verified facts, with local retry ownership."""

from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping

from sqlalchemy.engine import Engine

from app.catalog.non_pdf import NonPdfCatalogStore
from app.operational_state import OperationalStateStore, configured_store
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
    document_revision: int
    source_revision: int | None
    primary_revision: int
    content_revision: int | None
    prior_detected_format: str | None = None


class NonPdfExtractionRepository:
    """Own candidate selection and compact extraction state."""

    def __init__(
        self, database_url: str, *, schema: str = "monocorpus",
        runtime: OperationalStateStore | None = None,
    ) -> None:
        normalized = str(schema or "monocorpus").strip() or "monocorpus"
        if not _SCHEMA_RE.fullmatch(normalized):
            raise ValueError(f"Invalid database schema: {normalized!r}")
        self.runtime = runtime if runtime is not None else configured_store()
        self.engine: Engine = acquire_postgres_engine(
            str(database_url), schema=normalized
        )
        self.catalog = NonPdfCatalogStore(self.engine, schema=normalized)

    def dispose(self) -> None:
        release_postgres_engine(self.engine)

    def preflight(self) -> None:
        self.catalog.preflight()


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
        if only_md5s is not None:
            if not isinstance(only_md5s, (set, frozenset)) or any(
                not isinstance(md5, str) or re.fullmatch(r"[0-9a-f]{32}", md5) is None for md5 in only_md5s
            ):
                raise ValueError("only_md5s must be a set of lowercase source MD5s")
        rows = self.catalog.list_sources(only_md5s=only_md5s)
        runtime = self.runtime.list(NON_PDF_SCOPE)
        ranks = {}
        candidates = []
        for source in rows:
            row = dict(source)
            mime = str(row.get("mime_type") or "unknown").strip().lower()
            ranks[mime] = ranks.get(mime, 0) + 1
            if per_mime_limit is not None and ranks[mime] > per_mime_limit:
                continue
            local = runtime.get(row["md5"]) or {}
            local_time = local.get("updated_at")
            durable_time = row.get("durable_updated_at")
            use_local = bool(local_time and (durable_time is None or
                datetime.fromisoformat(local_time) >= durable_time)) and local.get("status") != "completed"
            started_at = local.get("attempt_started_at")
            if started_at and durable_time and row["durable_status"] in {"ready", "unsupported"}:
                # A committed result wins even if its acknowledgement/local
                # finalization failed after PostgreSQL committed the attempt.
                use_local = use_local and datetime.fromisoformat(started_at) > durable_time
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
        local = self.runtime.get(NON_PDF_SCOPE, candidate.md5)
        if not local or local["status"] != "processing":
            raise RuntimeError(f"Extraction attempt changed for {candidate.md5}")
        if verified_mime_type is not None:
            updated = self.catalog.record_verified_mime(
                asdict(candidate), detected_format=detected_format,
                version=extractor_version, mime=verified_mime_type,
            )
            candidate = replace(candidate, mime_type=verified_mime_type, document_revision=updated['document_revision'])
        if local["extractor_version"] != extractor_version:
            local["attempt_count"] = 1
        local.update(extractor_version=extractor_version, detected_format=detected_format,
                     updated_at=datetime.now(timezone.utc).isoformat())
        self.runtime.put(NON_PDF_SCOPE, candidate.md5, local)
        return candidate

    def start_attempt(self, md5, *, extractor_version, run_id):
        with self.runtime.transaction() as conn:
            row = conn.execute("SELECT payload_json FROM operational_items WHERE scope=? AND item_id=?",
                               (NON_PDF_SCOPE, md5)).fetchone()
            previous = json.loads(row["payload_json"]) if row else {}
            same = previous.get("extractor_version") == extractor_version
            now = datetime.now(timezone.utc).isoformat()
            self.runtime.put(NON_PDF_SCOPE, md5, {
                "extractor_version": extractor_version, "status": "processing",
                "attempt_count": int(previous.get("attempt_count", 0)) + 1 if same else 1,
                "detected_format": previous.get("detected_format") if same else None,
                "last_run_id": run_id, "error_text": None,
                "attempt_started_at": now, "updated_at": now,
            }, conn=conn)

    def mark_outcome(self, candidate, *, extractor_version, detected_format, status, run_id, error_text=None):
        if status not in _STATUSES:
            raise ValueError(f"Invalid non-PDF extraction status: {status!r}")
        local = self.runtime.get(NON_PDF_SCOPE, candidate.md5)
        if local is None:
            raise LookupError(f"Extraction state was not started for {candidate.md5}")
        if status == "unsupported":
            self.catalog.save_unsupported(
                asdict(candidate), version=extractor_version,
                detected_format=detected_format, reason=error_text,
            )
        local.update(extractor_version=extractor_version, detected_format=detected_format,
            status="completed" if status in {"unsupported", "ready"} else status,
            last_run_id=run_id, error_text=error_text if status not in {"unsupported", "ready"} else None,
            updated_at=datetime.now(timezone.utc).isoformat())
        self.runtime.put(NON_PDF_SCOPE, candidate.md5, local)

    def publication(self, candidate):
        return self.catalog.publication(asdict(candidate))

    def save_success(self, candidate, *, conn, extractor_version, detected_format, content_url, size, etag):
        self.catalog.save_success(
            conn, asdict(candidate), version=extractor_version, detected_format=detected_format,
            content_url=content_url, size=size, etag=etag,
        )

    @staticmethod
    def _candidate(row: Mapping[str, Any]) -> NonPdfCandidate:
        return NonPdfCandidate(
            md5=str(row.get("md5") or "").strip().lower(),
            mime_type=str(row.get("mime_type") or "").strip().lower(),
            source_path=str(row.get("ya_path") or ""),
            document_url=row['document_url'],
            primary_storage_size=int(row.get("primary_storage_size") or 0),
            content_url=row['content_url'],
            document_revision=row['document_revision'],
            source_revision=row['source_revision'],
            primary_revision=row['primary_revision'],
            content_revision=row['content_revision'],
            prior_detected_format=(
                str(row.get("prior_detected_format") or "").strip() or None
            ),
        )


__all__ = [
    "MAX_AUTOMATIC_ATTEMPTS",
    "NonPdfCandidate",
    "NonPdfExtractionRepository",
]
