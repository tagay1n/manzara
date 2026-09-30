"""Workbook extraction keeps visible cell tables as HTML in Markdown."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from app.modules.library.corrupt_document import CorruptDocumentError
from app.modules.library.non_pdf_extraction import prepare_extraction
from app.modules.library.non_pdf_formats import detect_document_format
from app.modules.library.non_pdf_rendering import render_markdown


def _xlsx(path: Path) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")


def test_spreadsheet_detection_uses_workbook_bytes(tmp_path: Path, monkeypatch) -> None:
    xlsx = tmp_path / "wrong.docx"
    _xlsx(xlsx)
    xls = tmp_path / "wrong.ppt"
    xls.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 504)
    monkeypatch.setattr(
        "app.modules.library.non_pdf_formats._root_ole_streams",
        lambda path: ["Workbook"] if path == xls else [],
    )

    assert detect_document_format(xlsx, source_path="wrong.docx") == "spreadsheet"
    assert detect_document_format(xls, source_path="wrong.ppt") == "spreadsheet"


def test_spreadsheet_keeps_only_sheet_headings_and_html_tables(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.xlsx"
    _xlsx(source)
    converted = tmp_path / "converted.html"
    converted.write_text(
        "<html><body><h1>Overview</h1><p><a href='#table0'>Visible</a></p>"
        "<h1>Sheet 1: Visible</h1><table><tr><td>Scientific text</td>"
        "<td data-sheets-formula='=1+1'>987654321</td></tr></table>"
        "<h1>Sheet 2: Other</h1><table><tr><td>Second sheet</td>"
        "<td><img src='chart.png'></td></tr></table></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_spreadsheet_to_html",
        lambda *_args, **_kwargs: converted,
    )

    prepared = prepare_extraction(
        source,
        workspace=tmp_path / "workspace",
        mime_type="application/vnd.ms-excel",
        source_path="wrong.xls",
    )
    markdown = render_markdown(prepared, asset_urls={})

    assert prepared.assets == ()
    assert markdown.count("<table") == 2
    assert "Sheet 1: Visible" in markdown
    assert "Second sheet" in markdown
    assert "Scientific text" in markdown
    assert "987654321" in markdown
    assert "Overview" not in markdown
    assert "=1+1" not in markdown
    assert "<img" not in markdown
    assert "| Scientific text" not in markdown


def test_broken_xlsx_member_fails_before_converter(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.xlsx"
    _xlsx(source)
    payload = bytearray(source.read_bytes())
    offset = payload.index(b"<workbook/>")
    payload[offset] = ord("X")
    source.write_bytes(payload)
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_spreadsheet_to_html",
        lambda *_args, **_kwargs: pytest.fail("converter reached"),
    )

    with pytest.raises(CorruptDocumentError, match="document_container"):
        prepare_extraction(
            source,
            workspace=tmp_path / "workspace",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            source_path="source.xlsx",
        )


def test_xlsx_without_zip_container_is_corrupt(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"not a ZIP workbook")

    with pytest.raises(CorruptDocumentError, match="document_container"):
        prepare_extraction(
            source,
            workspace=tmp_path / "workspace",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            source_path="source.xlsx",
        )


def test_single_sheet_export_gets_a_heading(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.xlsx"
    _xlsx(source)
    converted = tmp_path / "converted.html"
    converted.write_text(
        "<html><body><table><tr><td>Only sheet</td></tr></table></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_spreadsheet_to_html",
        lambda *_args, **_kwargs: converted,
    )

    prepared = prepare_extraction(
        source,
        workspace=tmp_path / "workspace",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        source_path="source.xlsx",
    )
    markdown = render_markdown(prepared, asset_urls={})

    assert markdown.startswith("# Sheet 1\n")
    assert "<table" in markdown
