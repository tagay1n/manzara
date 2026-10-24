"""Shared source records, extracted text, and PDF evidence for metadata tasks."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pymupdf
import requests

from app.modules.library.corrupt_document import (
    CorruptDocumentError,
    PasswordProtectedDocumentError,
)
from app.modules.library.djvu_slicing import select_edge_pages
from app.runtime_config import config_integer


@dataclass(frozen=True)
class MetadataExtractionCandidate:
    """One verified primary-storage document used as metadata evidence."""

    md5: str
    mime_type: str
    document_url: str
    content_url: str | None
    upstream_metadata: Mapping[str, Any] | None
    primary_storage_size: int
    source_filename: str = ""
    source_path: str = ""


def select_pdf_pages(page_count: int, *, first_pages: int | None = None, last_pages: int | None = None) -> list[int]:
    """Return unique first/last PDF page indexes in source order."""
    return select_edge_pages(page_count,
        first_pages=config_integer("metadata", "first_pages", minimum=0) if first_pages is None else first_pages,
        last_pages=config_integer("metadata", "last_pages", minimum=0) if last_pages is None else last_pages)


def load_text_slice(
    candidate: MetadataExtractionCandidate, *, workspace: Path
) -> str:
    """Read the source command's first Markdown characters from its ZIP."""
    if not candidate.content_url:
        raise ValueError(f"Document {candidate.md5} has no extracted content URL")
    doc_dir = workspace / candidate.md5
    doc_dir.mkdir(parents=True, exist_ok=True)
    archive_path = doc_dir / "content.zip"
    with requests.get(candidate.content_url, stream=True, timeout=(config_integer("metadata", "content_connect_timeout_seconds"), config_integer("metadata", "content_read_timeout_seconds"))) as response:
        response.raise_for_status()
        with archive_path.open("wb") as payload:
            for chunk in response.iter_content(chunk_size=config_integer("metadata", "content_chunk_bytes")):
                payload.write(chunk)
    member = f"{candidate.md5}.md"
    with zipfile.ZipFile(archive_path) as archive:
        with archive.open(member) as raw, io.TextIOWrapper(raw, encoding="utf-8") as handle:
            return handle.read(config_integer("metadata", "text_slice_chars"))


def create_pdf_slice(source: Path, destination: Path, *, first_pages: int | None = None, last_pages: int | None = None) -> int:
    """Create a first/last-page PDF slice and return its page count."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        pdf = pymupdf.open(source)
    except (pymupdf.EmptyFileError, pymupdf.FileDataError) as exc:
        raise CorruptDocumentError("pdf_open", str(exc)) from exc
    with pdf, pymupdf.open() as sliced:
        if pdf.needs_pass:
            raise PasswordProtectedDocumentError(
                f"Password-protected PDF cannot be read: {source}"
            )
        pages = select_pdf_pages(pdf.page_count, first_pages=first_pages, last_pages=last_pages)
        if not pages:
            raise CorruptDocumentError(
                "pdf_page_tree", f"PDF has no usable pages: {source}"
            )
        try:
            for page in pages:
                pdf.load_page(page)
                sliced.insert_pdf(pdf, from_page=page, to_page=page)
        except (RuntimeError, ValueError) as exc:
            raise CorruptDocumentError("pdf_page_read", str(exc)) from exc
        sliced.save(destination)
        return sliced.page_count
