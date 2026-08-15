"""Shared document sync filtering policy tests."""

from app.document_sync_filter import classify_document


def test_non_document_mime_is_filtered() -> None:
    decision = classify_document("disk:/documents/archive.zip", "application/zip")

    assert decision.accepted is False
    assert decision.reason == "non_document_mime"


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
