"""Shared document sync filtering policy tests."""

from app.modules.maintenance.document_sync_filter import classify_document


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
