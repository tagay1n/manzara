"""Library non pdf formats coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.modules.library.corrupt_document import CorruptDocumentError
from app.modules.library.non_pdf_extraction import (
    detect_document_format,
    prepare_extraction,
)
from app.modules.library.non_pdf_formats import _decode_text


def test_detection_prefers_source_bytes_over_wrong_mime_and_extension(
    tmp_path: Path,
) -> None:
    rtf = tmp_path / "wrong.docx"
    rtf.write_bytes(b"{\\rtf1\\ansi Rich text}")
    pdf = tmp_path / "wrong.txt"
    pdf.write_bytes(b"%PDF-1.7\n")
    html = tmp_path / "wrong.doc"
    html.write_text("<!doctype html><html><body>Tatar</body></html>")

    assert (
        detect_document_format(
            rtf, mime_type="application/msword", source_path="wrong.docx"
        )
        == "rtf"
    )
    assert (
        detect_document_format(pdf, mime_type="text/plain", source_path="wrong.txt")
        == "pdf"
    )
    assert (
        detect_document_format(
            html, mime_type="application/msword", source_path="wrong.doc"
        )
        == "html"
    )


def test_invalid_docx_container_is_classified_as_structural_corruption(
    tmp_path: Path,
) -> None:
    source = tmp_path / "broken.docx"
    source.write_bytes(b"not a ZIP container")

    with pytest.raises(CorruptDocumentError, match="document_container"):
        prepare_extraction(
            source,
            workspace=tmp_path / "workspace",
            mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            source_path="nested/broken.docx",
        )


def test_word_lock_file_is_classified_before_conversion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "~$temporary.doc"
    source.write_bytes(b"Word lock metadata")
    converter_called = False

    def convert(*_args, **_kwargs):
        nonlocal converter_called
        converter_called = True
        raise AssertionError("invalid source must not reach LibreOffice")

    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_to_docx", convert
    )

    with pytest.raises(CorruptDocumentError) as caught:
        prepare_extraction(
            source,
            workspace=tmp_path / "workspace",
            mime_type="application/msword",
            source_path="nested/~$temporary.doc",
        )

    assert caught.value.detector == "temporary_source"
    assert converter_called is False


def test_ole_detection_does_not_treat_thumbnail_cache_as_word_document(
    tmp_path: Path,
) -> None:
    ole_header = bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 504
    thumbnail_cache = tmp_path / "Thumbs.db"
    thumbnail_cache.write_bytes(ole_header + "256_a43e39b83acfaad6".encode("utf-16-le"))
    word = tmp_path / "mislabelled"
    word.write_bytes(ole_header + "WordDocument".encode("utf-16-le"))

    assert (
        detect_document_format(
            thumbnail_cache,
            mime_type="application/cdfv2",
            source_path="Thumbs.db",
        )
        == "compound"
    )
    assert (
        detect_document_format(
            word,
            mime_type="application/cdfv2",
            source_path="no-extension",
        )
        == "doc"
    )


def test_text_decoder_selects_cp866_for_dos_cyrillic() -> None:
    payload = "Program Demo; {Создаем новый тип данных}".encode("cp866")

    assert _decode_text(payload) == "Program Demo; {Создаем новый тип данных}"


def test_text_decoder_preserves_bom_marked_utf16() -> None:
    payload = "Татарча текст".encode("utf-16")

    assert _decode_text(payload) == "Татарча текст"
