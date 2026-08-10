"""Resumable Schema.org metadata extraction for verified Library documents."""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pymupdf
import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.document_storage import (
    DocumentStorageSettings,
    download_verified_primary_document,
    verify_primary_document_object,
)
from app.gemini_model_pool import GeminiModelResponseError
from app.modules.library.metadata_normalization import normalize_base_schema_org
from app.modules.library.metadata_prompt import (
    DEFINE_META_PROMPT_BODY,
    DEFINE_META_PROMPT_NON_PDF_HEADER,
    DEFINE_META_PROMPT_PDF_HEADER,
    DEFINE_META_PROMPT_TT_FOOTER,
)
from app.modules.library.runtime.metadata.schema import Book
from app.modules.runtime_shared_utils import load_upstream_metadata


TEXT_SLICE_CHARS = 20_000
PDF_EDGE_PAGES = 4
PROMPT_VERSION = "prompt.v2"
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUPPORTING_METADATA_FIELDS = (
    "author",
    "contributor",
    "publisher",
    "datePublished",
    "isbn",
    "inLanguage",
    "description",
    "numberOfPages",
    "bookEdition",
    "about",
    "genre",
    "audience",
    "suggestedMinAge",
    "isBasedOn",
)


def _has_value(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_has_value(item) for item in value)
    return True


def metadata_quality_issue(schema_org: Any) -> str | None:
    """Return why metadata is unusable, or ``None`` for a safe result."""
    if not isinstance(schema_org, Mapping):
        return "metadata is not an object"
    if not _has_value(schema_org.get("name")):
        return "metadata has no usable title"
    if not any(_has_value(schema_org.get(field)) for field in _SUPPORTING_METADATA_FIELDS):
        return "metadata has no usable bibliographic evidence beyond the title"
    return None


@dataclass(frozen=True)
class MetadataExtractionCandidate:
    """One verified primary-storage document pending metadata."""

    md5: str
    mime_type: str
    document_url: str
    content_url: str | None
    upstream_meta_url: str | None
    primary_storage_size: int
    attempts: tuple[dict[str, Any], ...]

    @property
    def attempted_models(self) -> set[str]:
        return {
            str(item.get("model") or "").strip()
            for item in self.attempts
            if str(item.get("model") or "").strip()
        }


@dataclass(frozen=True)
class MetadataRequest:
    """Prepared Gemini contents and optional local file upload."""

    contents: tuple[dict[str, str], ...]
    files: Mapping[Path, str]


class MetadataExtractionRepository:
    """Own metadata candidates, model checkpoints, and final DB writes."""

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
        self, *, limit: int | None = None
    ) -> list[MetadataExtractionCandidate]:
        """Return only pending documents with a verified primary object."""
        sql = """
            SELECT
                d.md5,
                d.mime_type,
                d.document_url,
                d.content_url,
                d.upstream_meta_url,
                d.primary_storage_size,
                state.attempts_json
            FROM document d
            LEFT JOIN metadata m ON m.md5 = d.md5
            LEFT JOIN library_metadata_extraction_state state ON state.md5 = d.md5
            WHERE (
                  m.md5 IS NULL
                  OR m.schema_org IS NULL
                  OR NULLIF(BTRIM(m.schema_org->>'name'), '') IS NULL
                  OR NOT EXISTS (
                      SELECT 1
                      FROM jsonb_each(m.schema_org::jsonb) AS signal(key, value)
                      WHERE signal.key = ANY(ARRAY[
                                'author', 'contributor', 'publisher', 'datePublished',
                                'isbn', 'inLanguage', 'description', 'numberOfPages',
                                'bookEdition', 'about', 'genre', 'audience',
                                'suggestedMinAge', 'isBasedOn'
                            ])
                        AND signal.value <> 'null'::jsonb
                        AND signal.value <> to_jsonb(''::text)
                        AND signal.value <> '[]'::jsonb
                        AND signal.value <> '{}'::jsonb
                  )
              )
              AND d.document_url IS NOT NULL
              AND d.primary_storage_size IS NOT NULL
              AND d.primary_storage_verified_at IS NOT NULL
              AND (
                  d.content_url IS NOT NULL
                  OR LOWER(COALESCE(d.mime_type, '')) = 'application/pdf'
              )
              AND (state.status IS NULL OR state.status = 'partial')
            ORDER BY d.md5 ASC
        """
        params: dict[str, Any] = {}
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = max(0, int(limit))
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        candidates: list[MetadataExtractionCandidate] = []
        seen: set[str] = set()
        for row in rows:
            candidate = self._candidate(row)
            if not candidate.md5:
                raise RuntimeError("Metadata candidate has no MD5")
            if candidate.md5 in seen:
                raise RuntimeError(
                    f"Duplicate document MD5 {candidate.md5}; refusing extraction"
                )
            seen.add(candidate.md5)
            candidates.append(candidate)
        return candidates

    @staticmethod
    def _candidate(row: Mapping[str, Any]) -> MetadataExtractionCandidate:
        raw_attempts = row.get("attempts_json")
        attempts = raw_attempts if isinstance(raw_attempts, list) else []
        return MetadataExtractionCandidate(
            md5=str(row.get("md5") or "").strip().lower(),
            mime_type=str(row.get("mime_type") or "").strip().lower(),
            document_url=str(row.get("document_url") or "").strip(),
            content_url=str(row.get("content_url") or "").strip() or None,
            upstream_meta_url=(
                str(row.get("upstream_meta_url") or "").strip() or None
            ),
            primary_storage_size=int(row.get("primary_storage_size") or 0),
            attempts=tuple(dict(item) for item in attempts if isinstance(item, dict)),
        )

    def record_model_failure(
        self,
        md5: str,
        *,
        model_name: str,
        kind: str,
        error: str,
        models: Sequence[str],
        run_id: int,
    ) -> None:
        """Checkpoint one content-level model failure exactly once."""
        attempt = {
            "model": str(model_name),
            "kind": str(kind),
            "error": str(error or "")[:4000],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        match = [{"model": str(model_name)}]
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO library_metadata_extraction_state (
                        md5, status, attempts_json, model_pool_json,
                        last_run_id, created_at, updated_at
                    ) VALUES (
                        :md5, 'partial', CAST(:attempt AS JSONB), CAST(:models AS JSONB),
                        :run_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (md5) DO UPDATE SET
                        attempts_json = CASE
                            WHEN library_metadata_extraction_state.attempts_json
                                 @> CAST(:match AS JSONB)
                                THEN library_metadata_extraction_state.attempts_json
                            ELSE library_metadata_extraction_state.attempts_json
                                 || CAST(:attempt AS JSONB)
                        END,
                        model_pool_json = EXCLUDED.model_pool_json,
                        last_run_id = EXCLUDED.last_run_id,
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "md5": str(md5),
                    "attempt": json.dumps([attempt], ensure_ascii=False),
                    "match": json.dumps(match, ensure_ascii=False),
                    "models": json.dumps(list(models), ensure_ascii=False),
                    "run_id": int(run_id),
                },
            )

    def mark_terminal(
        self,
        md5: str,
        *,
        models: Sequence[str],
        run_id: int,
        reason: str,
    ) -> None:
        """Exclude one document after every configured model failed."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO library_metadata_extraction_state (
                        md5, status, attempts_json, model_pool_json, last_run_id,
                        terminal_reason, created_at, updated_at
                    ) VALUES (
                        :md5, 'terminal', '[]'::jsonb, CAST(:models AS JSONB),
                        :run_id, :reason, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (md5) DO UPDATE SET
                        status = 'terminal',
                        model_pool_json = EXCLUDED.model_pool_json,
                        last_run_id = EXCLUDED.last_run_id,
                        terminal_reason = EXCLUDED.terminal_reason,
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "md5": str(md5),
                    "models": json.dumps(list(models), ensure_ascii=False),
                    "run_id": int(run_id),
                    "reason": str(reason or "")[:4000],
                },
            )

    def save_success(
        self,
        md5: str,
        *,
        schema_org: Mapping[str, Any],
        model_name: str,
    ) -> bool:
        """Persist usable metadata without replacing an existing usable payload."""
        language = str(schema_org.get("inLanguage") or "").strip() or None
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT d.md5, m.schema_org
                    FROM document d
                    LEFT JOIN metadata m ON m.md5 = d.md5
                    WHERE d.md5 = :md5
                    FOR UPDATE OF d
                    """
                ),
                {"md5": str(md5)},
            ).mappings().all()
            if len(rows) != 1:
                raise RuntimeError(
                    f"Document MD5 {md5} matched {len(rows)} rows; refusing metadata write"
                )
            existing_schema_org = rows[0].get("schema_org")
            if existing_schema_org is not None and metadata_quality_issue(
                existing_schema_org
            ) is None:
                conn.execute(
                    text("DELETE FROM library_metadata_extraction_state WHERE md5 = :md5"),
                    {"md5": str(md5)},
                )
                return False
            if issue := metadata_quality_issue(schema_org):
                raise ValueError(f"Refusing low-quality metadata write: {issue}")

            stored = conn.execute(
                text(
                    """
                    INSERT INTO metadata (md5, schema_org)
                    VALUES (:md5, CAST(:schema_org AS JSONB))
                    ON CONFLICT (md5) DO UPDATE SET
                        schema_org = EXCLUDED.schema_org
                    """
                ),
                {
                    "md5": str(md5),
                    "schema_org": json.dumps(dict(schema_org), ensure_ascii=False),
                },
            )
            if int(stored.rowcount or 0) != 1:
                raise RuntimeError(f"Metadata write did not persist for {md5}")

            updated = conn.execute(
                text(
                    """
                    UPDATE document
                    SET language = COALESCE(:language, language),
                        meta_extraction_method = :method
                    WHERE md5 = :md5
                    """
                ),
                {
                    "md5": str(md5),
                    "language": language,
                    "method": f"{model_name}/{PROMPT_VERSION}",
                },
            )
            if int(updated.rowcount or 0) != 1:
                raise RuntimeError(f"Document metadata marker update failed for {md5}")
            conn.execute(
                text("DELETE FROM library_metadata_extraction_state WHERE md5 = :md5"),
                {"md5": str(md5)},
            )
        return True


def select_pdf_pages(page_count: int, *, edge_pages: int = PDF_EDGE_PAGES) -> list[int]:
    """Return unique first/last PDF page indexes in source order."""
    count = max(0, int(page_count))
    edge = max(1, int(edge_pages))
    return sorted(set(range(min(edge, count))) | set(range(max(0, count - edge), count)))


def build_text_prompt(content: str) -> list[dict[str, str]]:
    """Build the unchanged monocorpus prompt for extracted text."""
    return [
        {"text": DEFINE_META_PROMPT_NON_PDF_HEADER.format(n=len(content))},
        {"text": DEFINE_META_PROMPT_BODY},
        {"text": DEFINE_META_PROMPT_TT_FOOTER},
        {"text": "Now, extract metadata from the following extraction from the document"},
        {"text": content},
    ]


def build_pdf_prompt(
    slice_page_count: int, *, upstream_metadata: str | None = None
) -> list[dict[str, str]]:
    """Build the unchanged monocorpus prompt for a representative PDF slice."""
    prompt = [
        {"text": DEFINE_META_PROMPT_PDF_HEADER.format(n=int(slice_page_count / 2))},
        {"text": DEFINE_META_PROMPT_BODY},
        {"text": DEFINE_META_PROMPT_TT_FOOTER},
    ]
    if upstream_metadata:
        prompt.extend(
            [
                {
                    "text": "📌 In addition to the content of the document, you are also provided with external metadata in JSON format. This metadata comes from other sources and should be treated as valid and trustworthy. Consider it alongside the doc content as if it were extracted from the document itself:"
                },
                {"text": upstream_metadata},
            ]
        )
    prompt.append({"text": "Now, extract metadata from the following document"})
    return prompt


def load_text_slice(
    candidate: MetadataExtractionCandidate, *, workspace: Path
) -> str:
    """Read the source command's first Markdown characters from its ZIP."""
    if not candidate.content_url:
        raise ValueError(f"Document {candidate.md5} has no extracted content URL")
    doc_dir = workspace / candidate.md5
    doc_dir.mkdir(parents=True, exist_ok=True)
    archive_path = doc_dir / "content.zip"
    with requests.get(candidate.content_url, stream=True, timeout=(15, 120)) as response:
        response.raise_for_status()
        with archive_path.open("wb") as payload:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                payload.write(chunk)
    member = f"{candidate.md5}.md"
    with zipfile.ZipFile(archive_path) as archive:
        with archive.open(member) as raw, io.TextIOWrapper(raw, encoding="utf-8") as handle:
            return handle.read(TEXT_SLICE_CHARS)


def create_pdf_slice(source: Path, destination: Path) -> int:
    """Create a first/last-page PDF slice and return its page count."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source) as pdf, pymupdf.open() as sliced:
        pages = select_pdf_pages(pdf.page_count)
        if not pages:
            raise ValueError(f"PDF has no pages: {source}")
        for page in pages:
            sliced.insert_pdf(pdf, from_page=page, to_page=page)
        sliced.save(destination)
        return sliced.page_count


def prepare_metadata_request(
    candidate: MetadataExtractionCandidate,
    *,
    workspace: Path,
    storage: DocumentStorageSettings,
    primary_s3: Any,
) -> MetadataRequest:
    """Prepare one text or Backblaze-only PDF request."""
    if candidate.content_url:
        verify_primary_document_object(
            settings=storage,
            s3=primary_s3,
            document_url=candidate.document_url,
            expected_size=candidate.primary_storage_size,
        )
        content = load_text_slice(candidate, workspace=workspace)
        return MetadataRequest(tuple(build_text_prompt(content)), {})

    if candidate.mime_type != "application/pdf":
        raise ValueError(f"Unsupported metadata source MIME: {candidate.mime_type}")
    doc_dir = workspace / candidate.md5
    source = download_verified_primary_document(
        settings=storage,
        s3=primary_s3,
        document_url=candidate.document_url,
        expected_md5=candidate.md5,
        expected_size=candidate.primary_storage_size,
        destination=doc_dir / f"{candidate.md5}.pdf",
    )
    slice_path = doc_dir / "slice-for-meta.pdf"
    page_count = create_pdf_slice(source, slice_path)
    upstream = load_upstream_metadata(candidate.upstream_meta_url, candidate.md5)
    return MetadataRequest(
        tuple(build_pdf_prompt(page_count, upstream_metadata=upstream)),
        {slice_path: candidate.mime_type},
    )


def parse_metadata_response(raw_response: Any) -> dict[str, Any]:
    """Validate and normalize one Gemini response."""
    if not isinstance(raw_response, str) or not raw_response.strip():
        raise GeminiModelResponseError("Gemini returned an empty metadata response")
    try:
        metadata = Book.model_validate_json(raw_response)
    except Exception as exc:
        raise GeminiModelResponseError(f"Invalid metadata JSON: {exc}") from exc
    schema_org = json.loads(
        metadata.model_dump_json(
            by_alias=True,
            exclude_none=True,
            exclude_unset=True,
            ensure_ascii=False,
        )
    )
    normalized = normalize_base_schema_org(schema_org)
    if issue := metadata_quality_issue(normalized):
        raise GeminiModelResponseError(f"Gemini returned no usable metadata: {issue}")
    return normalized


__all__ = [
    "MetadataExtractionCandidate",
    "MetadataExtractionRepository",
    "MetadataRequest",
    "PROMPT_VERSION",
    "build_pdf_prompt",
    "build_text_prompt",
    "metadata_quality_issue",
    "parse_metadata_response",
    "prepare_metadata_request",
    "select_pdf_pages",
]
