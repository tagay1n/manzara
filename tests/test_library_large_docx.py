"""Focused coverage for streaming oversized Word XML through compact HTML."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from PIL import Image

from app.modules.library.non_pdf_extraction import prepare_extraction
from app.modules.library.non_pdf_large_docx import maybe_convert_large_docx_to_html
from app.modules.library.non_pdf_rendering import (
    render_markdown,
    validate_rendered_markdown,
)


def test_large_docx_preserves_prose_and_embedded_image(
    tmp_path: Path, monkeypatch
) -> None:
    from app.modules.library import non_pdf_large_docx

    source = tmp_path / "book.docx"
    image = tmp_path / "cover.png"
    Image.new("RGB", (3, 3), "blue").save(image)
    document_xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<w:body><w:p><w:r><w:t>First paragraph.</w:t></w:r></w:p>'
        '<w:p><w:r><a:blip r:embed="rId1"/></w:r></w:p>'
        '<w:p><w:r><w:t>Second paragraph.</w:t></w:r></w:p>'
        '</w:body></w:document>'
    )
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
        'Target="media/cover.png"/></Relationships>'
    )
    with ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", relationships)
        archive.write(image, "word/media/cover.png")
    monkeypatch.setattr(non_pdf_large_docx, "LARGE_DOCX_XML_BYTES", 1)

    prepared = prepare_extraction(
        source,
        workspace=tmp_path / "workspace",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_path="book.docx",
    )
    assert prepared.detected_format == "docx"
    assert len(prepared.assets) == 1
    urls = {prepared.assets[0].source_ref: "https://example.test/cover.png"}
    markdown = render_markdown(prepared, asset_urls=urls)
    assert "First paragraph." in markdown
    assert "Second paragraph." in markdown
    assert 'src="https://example.test/cover.png"' in markdown
    assert validate_rendered_markdown(prepared, markdown, asset_urls=urls)["passed"]


def test_large_docx_with_table_keeps_regular_reader(tmp_path: Path, monkeypatch) -> None:
    from app.modules.library import non_pdf_large_docx

    source = tmp_path / "table.docx"
    with ZipFile(source, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell</w:t></w:r>'
            '</w:p></w:tc></w:tr></w:tbl></w:body></w:document>',
        )
    monkeypatch.setattr(non_pdf_large_docx, "LARGE_DOCX_XML_BYTES", 1)

    assert maybe_convert_large_docx_to_html(source, workspace=tmp_path) is None
    assert not (tmp_path / "large-docx-html" / "source.html").exists()
