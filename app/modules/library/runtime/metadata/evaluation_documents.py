"""Evaluation source cache, content excerpts, PDF slices, and prompt artifacts."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pymupdf as fitz
import requests

from app.document_storage import (
    DEFAULT_DOCUMENT_CACHE_MAX_BYTES,
    load_document_storage_settings,
    materialize_cached_document,
    resolve_document_download_url,
)
from app.modules.library.runtime.dirs import Dirs
from app.modules.library.runtime.integrations.s3 import (
    create_document_session,
    create_session,
)
from app.modules.runtime_shared_utils import get_in_workdir

from .evaluation_text import _build_content_excerpt
from .evaluation_types import EvaluationTask

EVAL_PDF_SLICE_SIZE = 3


def _ensure_local_zip(
    md5: str, content_url: str, s3client, fallback_bucket: str
) -> tuple[str, str, str]:
    local_zip = get_in_workdir(Dirs.CONTENT, file=f"{md5}.zip")
    bucket, key = _parse_s3_location(content_url, fallback_bucket, f"{md5}.zip")
    if not os.path.exists(local_zip):
        s3client.download_file(bucket, key, local_zip)
    if not os.path.exists(local_zip):
        raise FileNotFoundError(local_zip)
    return local_zip, bucket, key


def _parse_s3_location(
    content_url: str, fallback_bucket: str, fallback_key: str
) -> tuple[str, str]:
    if content_url:
        try:
            parsed = urlparse(content_url)
            if parsed.scheme and parsed.netloc:
                path = parsed.path.lstrip("/")
                if path:
                    parts = path.split("/", 1)
                    bucket = parts[0]
                    key = parts[1] if len(parts) > 1 and parts[1] else fallback_key
                    return bucket, key
        except Exception:
            pass
    return fallback_bucket, fallback_key


def _read_markdown_from_zip(zip_path: str, md5: str) -> str:
    with zipfile.ZipFile(zip_path, "r") as zf:
        md_name = f"{md5}.md"
        names = zf.namelist()
        if md_name not in names:
            md_candidates = [n for n in names if n.lower().endswith(".md")]
            if not md_candidates:
                raise ValueError("No markdown file found in archive")
            md_name = md_candidates[0]
        return zf.read(md_name).decode("utf-8", errors="replace")


def _insert_page_ranges(
    source_pdf: fitz.Document, target_pdf: fitz.Document, pages: list[int]
) -> None:
    if not pages:
        return
    pages = sorted(set(pages))
    start = pages[0]
    prev = pages[0]
    for current in pages[1:]:
        if current == prev + 1:
            prev = current
            continue
        target_pdf.insert_pdf(source_pdf, from_page=start, to_page=prev)
        start = current
        prev = current
    target_pdf.insert_pdf(source_pdf, from_page=start, to_page=prev)


def _download_file(url: str, local_path: str) -> None:
    with requests.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with open(local_path, "wb") as fh:
            for chunk in response.iter_content(1024 * 64):
                if chunk:
                    fh.write(chunk)


def _resolve_doc_source_url(doc: Any, config: dict, s3client: Any) -> str | None:
    storage = load_document_storage_settings(config)
    return resolve_document_download_url(
        document_url=doc.document_url,
        fallback_url=doc.ya_public_url,
        encryption_key=config["encryption_key"],
        endpoint_url=storage.primary.endpoint_url,
        private_bucket=storage.private_bucket,
        s3=s3client,
    )


def _ensure_pdf_in_shared_cache(
    doc: Any,
    config: dict,
    s3client: Any,
) -> str:
    storage = load_document_storage_settings(config)

    def download(destination: Path) -> None:
        source_url = _resolve_doc_source_url(doc, config, s3client)
        if not source_url:
            raise ValueError(f"Document has no downloadable source: {doc.md5}")
        _download_file(source_url, str(destination))

    return str(
        materialize_cached_document(
            cache_path=storage.cache_path,
            expected_md5=doc.md5,
            extension=".pdf",
            download=download,
            cache_max_bytes=getattr(
                storage,
                "cache_max_bytes",
                DEFAULT_DOCUMENT_CACHE_MAX_BYTES,
            ),
        )
    )


def _fallback_pdf_page_count(doc: Any, config: dict) -> int | None:
    if doc.mime_type != "application/pdf":
        return None
    local_pdf = _ensure_pdf_in_shared_cache(
        doc,
        config,
        create_document_session(config),
    )
    with fitz.open(local_pdf) as pdf:
        count = int(pdf.page_count)
        return count if count > 0 else None


class EvaluationDocuments:
    """Worker-local document clients, evidence preparation, and prompt artifacts."""

    def __init__(self, *, config: dict, excerpt_chars: int, log):
        self.config = config
        self.excerpt_chars = excerpt_chars
        self.log = log
        self._document_s3client = None
        self._yandex_s3client = None

    def load_content_excerpt(self, doc: EvaluationTask) -> str | None:
        if self.excerpt_chars <= 0:
            return None
        if not doc.content_url:
            return None
        try:
            content_bucket = self.config["yandex"]["cloud"]["bucket"]["content"]
            local_zip = get_in_workdir(Dirs.CONTENT, file=f"{doc.md5}.zip")
            if not os.path.exists(local_zip):
                s3client = self._get_yandex_s3client()
                local_zip, _, _ = _ensure_local_zip(
                    doc.md5, doc.content_url, s3client, content_bucket
                )
            markdown = _read_markdown_from_zip(local_zip, doc.md5)
            return _build_content_excerpt(markdown, self.excerpt_chars)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not build excerpt for {doc.md5}: {exc}")
            return None

    def dump_prompt(self, md5: str, prompt: list[dict[str, Any]]) -> None:
        try:
            prompt_path = get_in_workdir(
                Dirs.PROMPTS, file=f"{md5}-meta-eval-prompt.txt"
            )
            with open(prompt_path, "w") as fh:
                fh.write(json.dumps(prompt, ensure_ascii=False, indent=4))
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not dump eval prompt for {md5}: {exc}")

    def prepare_pdf_slice(self, doc: EvaluationTask) -> str | None:
        if doc.mime_type != "application/pdf":
            return None
        try:
            local_pdf = _ensure_pdf_in_shared_cache(
                doc,
                self.config,
                self._get_document_s3client(),
            )

            slice_path = get_in_workdir(
                Dirs.DOC_SLICES, doc.md5, file="slice-for-eval.pdf"
            )
            with fitz.open(local_pdf) as pdf_doc, fitz.open() as doc_slice:
                pages = list(range(0, pdf_doc.page_count))
                pages = sorted(
                    list(
                        set(pages[:EVAL_PDF_SLICE_SIZE] + pages[-EVAL_PDF_SLICE_SIZE:])
                    )
                )
                _insert_page_ranges(pdf_doc, doc_slice, pages)
                doc_slice.save(slice_path)
            return slice_path
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not prepare PDF slice for {doc.md5}: {exc}")
            return None

    def _get_document_s3client(self):
        if self._document_s3client is None:
            self._document_s3client = create_document_session(self.config)
        return self._document_s3client

    def _get_yandex_s3client(self):
        if self._yandex_s3client is None:
            self._yandex_s3client = create_session(self.config)
        return self._yandex_s3client
