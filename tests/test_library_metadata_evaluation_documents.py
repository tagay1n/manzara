"""Library metadata evaluation documents coverage."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

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
