"""Shared document sync filtering policy tests."""

from app.document_sync_filter import classify_document


def test_non_document_mime_is_filtered() -> None:
    decision = classify_document("disk:/documents/archive.zip", "application/zip")

    assert decision.accepted is False
    assert decision.reason == "non_document_mime"


def test_pascal_source_is_non_document_by_mime_or_suffix() -> None:
    by_mime = classify_document("disk:/documents/code.bin", "text/pascal")
    by_suffix = classify_document("disk:/documents/code.PAS", "text/plain")

    assert by_mime.accepted is False
    assert by_mime.reason == "non_document_mime"
    assert by_suffix.accepted is False
    assert by_suffix.reason == "non_document_suffix"


def test_octet_stream_pdf_is_normalized_and_kept() -> None:
    decision = classify_document(
        "disk:/documents/book.pdf", "application/octet-stream"
    )

    assert decision.accepted is True
    assert decision.mime_type == "application/pdf"


def test_annas_archive_text_artifact_is_filtered() -> None:
    decision = classify_document(
        "disk:/neurotatarlar/kitaplar/monocorpus/Anna's archive/123/0001.txt",
        "text/plain",
    )

    assert decision.accepted is False
    assert decision.reason == "annas_archive_text"


def test_ilbyak_html_artifact_is_filtered_with_intermediate_directories() -> None:
    decision = classify_document(
        "disk:/neurotatarlar/kitaplar/monocorpus/_1st_priority_for_OCR/"
        "source/random_files_thru_yandex_search/ilbyak-school.narod.ru/page.htm",
        "text/html",
    )

    assert decision.accepted is False
    assert decision.reason == "ilbyak_html"


def test_known_non_document_suffix_is_filtered() -> None:
    assert classify_document("disk:/documents/annotation.eaf", "text/troff").accepted is False
    assert classify_document("disk:/documents/score.musx", "application/octet-stream").accepted is False


def test_windows_shortcut_is_filtered_by_suffix_or_mime_type() -> None:
    by_suffix = classify_document(
        "disk:/documents/47 - Ярлык.lnk",
        "application/pdf",
    )
    by_mime = classify_document(
        "disk:/documents/shortcut.bin",
        "application/x-ms-shortcut",
    )

    assert by_suffix.accepted is False
    assert by_suffix.reason == "non_document_suffix"
    assert by_mime.accepted is False
    assert by_mime.reason == "non_document_mime"


def test_djvu_is_kept_despite_image_mime_prefix() -> None:
    decision = classify_document("disk:/documents/book.djvu", "image/vnd.djvu")

    assert decision.accepted is True
    assert decision.mime_type == "image/vnd.djvu"


def test_word_documents_are_normalized_from_octet_stream_and_kept() -> None:
    doc = classify_document("disk:/documents/book.doc", "application/octet-stream")
    docx = classify_document("disk:/documents/book.docx", "application/octet-stream")

    assert doc.accepted is True
    assert doc.mime_type == "application/msword"
    assert docx.accepted is True
    assert docx.mime_type.endswith("wordprocessingml.document")


def test_excel_workbooks_are_kept_by_mime_or_suffix() -> None:
    xls = classify_document("disk:/documents/data.xls", "application/octet-stream")
    xlsx = classify_document(
        "disk:/documents/data.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    assert xls.accepted is True
    assert xls.mime_type == "application/vnd.ms-excel"
    assert xlsx.accepted is True


def test_mobi_source_is_kept_when_catalog_mime_is_octet_stream() -> None:
    decision = classify_document("disk:/books/story.mobi", "application/octet-stream")

    assert decision.accepted is True
    assert decision.mime_type == "application/x-mobipocket-ebook"


def test_known_document_suffix_recovers_missing_mime() -> None:
    decision = classify_document("disk:/documents/book.djvu", "")

    assert decision.accepted is True
    assert decision.mime_type == "image/vnd.djvu"


def test_media_types_missing_from_legacy_exact_list_are_filtered() -> None:
    assert classify_document("disk:/media/movie.wmv", "video/x-ms-wmv").accepted is False
    assert classify_document("disk:/media/audio.wma", "audio/x-ms-wma").accepted is False


def test_unknown_octet_stream_is_filtered() -> None:
    decision = classify_document("disk:/fonts/typeface.ttf", "application/octet-stream")

    assert decision.accepted is False
    assert decision.reason == "non_document_unknown_binary"
