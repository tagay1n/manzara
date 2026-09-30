"""ODT HTML and MOBI EPUB extraction contracts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image

from app.modules.library.non_pdf_extraction import prepare_extraction
from app.modules.library.non_pdf_formats import detect_document_format
from app.modules.library.non_pdf_rendering import render_markdown
from app.modules.library.non_pdf_types import (
    ConverterCommandError,
    extractor_version_for_format,
)


def _odt(path: Path) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", "<office:document-content/>")


def _mobi(path: Path) -> None:
    path.write_bytes(b"X" * 60 + b"BOOKMOBI" + b"X" * 256)


def test_mobi_detection_prefers_signature_over_wrong_catalog_hints(tmp_path: Path) -> None:
    source = tmp_path / "wrong.doc"
    _mobi(source)

    assert detect_document_format(
        source, mime_type="application/msword", source_path="wrong.doc"
    ) == "mobi"


def test_book_recipes_have_independent_versions() -> None:
    assert extractor_version_for_format("odt") == "nonpdf.odt.v1"
    assert extractor_version_for_format("mobi") == "nonpdf.mobi.v1"


def test_odt_html_keeps_prose_html_tables_and_sidecar_images(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.odt"
    _odt(source)
    converted_dir = tmp_path / "workspace" / "converted"
    converted_dir.mkdir(parents=True)
    converted = converted_dir / "source.html"
    Image.new("RGB", (2, 2), "white").save(converted_dir / "diagram.png")
    converted.write_text(
        "<html><body><h1>Heading</h1><p>Document prose.</p>"
        "<table><tr><td>Cell content</td></tr></table>"
        "<p><img src='diagram.png' alt='Diagram'></p></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_odt_to_html",
        lambda *_args, **_kwargs: converted,
    )

    prepared = prepare_extraction(
        source,
        workspace=tmp_path / "workspace",
        mime_type="application/vnd.oasis.opendocument.text",
        source_path="source.odt",
    )
    assert prepared.detected_format == "odt"
    assert len(prepared.assets) == 1
    markdown = render_markdown(
        prepared,
        asset_urls={prepared.assets[0].source_ref: "https://example.test/diagram.png"},
    )
    assert "Document prose" in markdown
    assert "<table" in markdown
    assert "Cell content" in markdown
    assert "| Cell content" not in markdown
    assert '<img alt="Diagram" src="https://example.test/diagram.png"' in markdown


def test_mobi_uses_converted_epub_but_retains_source_format(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.mobi"
    _mobi(source)
    converted = tmp_path / "converted.epub"
    subprocess.run(
        ["pandoc", "-f", "markdown", "-t", "epub", "-o", str(converted)],
        input="# Chapter\n\nBook content.\n",
        text=True,
        check=True,
    )
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_mobi_to_epub",
        lambda *_args, **_kwargs: converted,
    )

    prepared = prepare_extraction(
        source,
        workspace=tmp_path / "workspace",
        mime_type="application/x-mobipocket-ebook",
        source_path="source.mobi",
    )
    assert prepared.detected_format == "mobi"
    assert "Book content" in render_markdown(prepared, asset_urls={})


def test_invalid_converted_mobi_epub_is_operational_failure(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.mobi"
    _mobi(source)
    converted = tmp_path / "invalid.epub"
    with ZipFile(converted, "w") as archive:
        archive.writestr("unrelated.txt", "not an EPUB")
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_mobi_to_epub",
        lambda *_args, **_kwargs: converted,
    )

    with pytest.raises(ConverterCommandError, match="EPUB"):
        prepare_extraction(
            source,
            workspace=tmp_path / "workspace",
            mime_type="application/x-mobipocket-ebook",
            source_path="source.mobi",
        )


def test_missing_calibre_is_operational_failure(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.mobi"
    _mobi(source)
    monkeypatch.setattr(
        "app.modules.library.non_pdf_book_converters.shutil.which",
        lambda _name: None,
    )

    with pytest.raises(ConverterCommandError, match="ebook-convert"):
        prepare_extraction(
            source,
            workspace=tmp_path / "workspace",
            mime_type="application/x-mobipocket-ebook",
            source_path="source.mobi",
        )
