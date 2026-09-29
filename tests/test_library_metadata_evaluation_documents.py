"""Library metadata evaluation documents coverage."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from pathlib import Path

from app.modules.library.runtime.metadata import (
    evaluation_documents as evaluation_documents_module,
)


def test_evaluation_replaces_invalid_pdf_in_configured_shared_cache(
    monkeypatch, tmp_path, evaluation_document
) -> None:
    content = b"verified-evaluation-pdf"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    cached = tmp_path / f"{digest}.pdf"
    cached.write_bytes(b"corrupt")
    doc = evaluation_document()
    doc.md5 = digest
    downloads: list[str] = []

    monkeypatch.setattr(
        evaluation_documents_module,
        "load_document_storage_settings",
        lambda _config: SimpleNamespace(cache_path=tmp_path),
    )
    monkeypatch.setattr(
        evaluation_documents_module,
        "_resolve_doc_source_url",
        lambda *_args: "https://example.test/document.pdf",
    )

    def download(_url, local_path):  # noqa: ANN001
        downloads.append(str(local_path))
        evaluation_documents_module.Path(local_path).write_bytes(content)

    monkeypatch.setattr(evaluation_documents_module, "_download_file", download)

    result = evaluation_documents_module._ensure_pdf_in_shared_cache(doc, {}, object())

    assert result == str(cached)
    assert cached.read_bytes() == content
    assert len(downloads) == 1


def test_djvu_evaluation_reuses_verified_cache_and_ignores_content(
    monkeypatch, tmp_path: Path, evaluation_document
) -> None:
    content = b"cached-djvu"
    digest = hashlib.md5(content).hexdigest()  # noqa: S324
    cached = tmp_path / f"{digest}.djv"
    cached.write_bytes(content)
    doc = evaluation_document()
    doc.md5 = digest
    doc.mime_type = "image/vnd.djvu"
    doc.content_url = "https://example.test/content.zip"
    doc.document_url = f"https://example.test/{digest}.djvu"
    seen_sources: list[Path] = []
    documents = evaluation_documents_module.EvaluationDocuments(
        config={}, excerpt_chars=100, log=lambda _message: None
    )
    monkeypatch.setattr(
        evaluation_documents_module,
        "load_document_storage_settings",
        lambda _config: SimpleNamespace(cache_path=tmp_path, cache_max_bytes=10**9),
    )
    monkeypatch.setattr(documents, "_get_document_s3client", lambda: object())
    monkeypatch.setattr(
        evaluation_documents_module,
        "get_in_workdir",
        lambda *_args, **_kwargs: str(tmp_path / "slice.pdf"),
    )

    def create_slice(source: Path, destination: Path, *, edge_pages: int) -> int:
        seen_sources.append(source)
        assert edge_pages == 3
        destination.write_bytes(b"pdf")
        return 6

    monkeypatch.setattr(evaluation_documents_module, "create_djvu_slice", create_slice)

    assert documents.load_content_excerpt(doc) is None
    assert documents.prepare_pdf_slice(doc) == str(tmp_path / "slice.pdf")
    assert seen_sources == [cached]
