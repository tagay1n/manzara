"""Rich non-PDF detection and structured extraction coverage."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import time
import zipfile

from PIL import Image
import pytest

from app.modules.library.non_pdf_extraction import (
    ConverterCommandError,
    EXTRACTOR_VERSION,
    ExtractedAsset,
    PreparedExtraction,
    _decode_text,
    _run,
    _collect_assets,
    detect_document_format,
    prepare_extraction,
    render_markdown,
    validate_rendered_markdown,
)
from app.modules.library.corrupt_document import CorruptDocumentError
from app.modules.library.google_doc_conversion import GoogleDriveDocxConverter
from app.modules.library.non_pdf_repository import (
    NonPdfCandidate,
    NonPdfExtractionRepository,
)
from app.modules.library.runtime.run_extract_non_pdf import (
    _delete_stale_assets,
    _failure_status,
    _write_content_archive,
    run_extraction,
)
from app.modules.library.tasks import library_task_definitions


def test_task_catalog_includes_extract_non_pdf(tmp_path: Path) -> None:
    task = {
        item["task_id"]: item for item in library_task_definitions(app_root=tmp_path)
    }["library.extract_non_pdf"]

    assert task["panel_id"] == "library"
    assert task["title"] == "Extract non-pdf"
    assert "run_extract_non_pdf" in task["command"]["value"]
    assert "--per-mime-limit" not in task["command"]["value"]
    assert EXTRACTOR_VERSION == "nonpdf.v7"


def test_migration_allows_schema_without_external_document_catalog() -> None:
    migration = Path(
        "alembic/versions/20260825_0034_add_non_pdf_extraction_state.py"
    ).read_text(encoding="utf-8")

    assert "IF to_regclass" in migration
    assert "ADD CONSTRAINT fk_library_non_pdf_extraction_document" in migration
    create_table = migration.split("CREATE TABLE", 1)[1].split('"""', 1)[0]
    assert "FOREIGN KEY (md5)" not in create_table


def test_retry_policy_migration_defers_deterministic_existing_failures() -> None:
    migration = Path(
        "alembic/versions/20260826_0035_add_non_pdf_deferred_status.py"
    ).read_text(encoding="utf-8")

    assert "'deferred'" in migration
    assert "Extracted document contains only images; OCR required" in migration
    assert "Rendered Markdown validation failed" in migration
    assert "LibreOffice produced 0 DOCX files" in migration
    assert "couldn''t unpack docx container" in migration


def test_detection_prefers_source_bytes_over_wrong_mime_and_extension(
    tmp_path: Path,
) -> None:
    rtf = tmp_path / "wrong.docx"
    rtf.write_bytes(b"{\\rtf1\\ansi Rich text}")
    pdf = tmp_path / "wrong.txt"
    pdf.write_bytes(b"%PDF-1.7\n")
    html = tmp_path / "wrong.doc"
    html.write_text("<!doctype html><html><body>Tatar</body></html>")

    assert detect_document_format(
        rtf, mime_type="application/msword", source_path="wrong.docx"
    ) == "rtf"
    assert detect_document_format(
        pdf, mime_type="text/plain", source_path="wrong.txt"
    ) == "pdf"
    assert detect_document_format(
        html, mime_type="application/msword", source_path="wrong.doc"
    ) == "html"


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


def test_legacy_doc_uses_google_fallback_for_invalid_libreoffice_output(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "source.doc"
    source.write_bytes(
        bytes.fromhex("d0cf11e0a1b11ae1")
        + b"\x00" * 504
        + "WordDocument".encode("utf-16-le")
    )
    invalid_docx = tmp_path / "invalid.docx"
    with zipfile.ZipFile(invalid_docx, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    valid_docx = tmp_path / "google.docx"
    subprocess.run(
        ["pandoc", "-f", "markdown", "-t", "docx", "-o", str(valid_docx)],
        input="# Google converted\n",
        text=True,
        check=True,
    )
    fallback_calls: list[tuple[Path, str]] = []

    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_to_docx",
        lambda *_args, **_kwargs: invalid_docx,
    )

    def google_fallback(
        fallback_source: Path, *, workspace: Path, detected_format: str
    ) -> Path:
        fallback_calls.append((fallback_source, detected_format))
        return valid_docx

    prepared = prepare_extraction(
        source,
        workspace=tmp_path / "workspace",
        mime_type="application/msword",
        source_path="source.doc",
        legacy_doc_converter=google_fallback,
    )

    assert prepared.detected_format == "doc"
    assert prepared.legacy_conversion == "google_drive"
    assert fallback_calls == [(source, "doc")]


def test_legacy_doc_normalizes_valid_libreoffice_docx_before_pandoc(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "source.doc"
    source.write_bytes(
        bytes.fromhex("d0cf11e0a1b11ae1")
        + b"\x00" * 504
        + "WordDocument".encode("utf-16-le")
    )
    converted = tmp_path / "converted.docx"
    subprocess.run(
        ["pandoc", "-f", "markdown", "-t", "docx", "-o", str(converted)],
        input="# LibreOffice converted\n",
        text=True,
        check=True,
    )
    normalized_calls: list[Path] = []

    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_to_docx",
        lambda *_args, **_kwargs: converted,
    )

    def normalize(path: Path, *, workspace: Path) -> Path:
        normalized_calls.append(path)
        return path

    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._normalize_docx_archive",
        normalize,
    )

    prepared = prepare_extraction(
        source,
        workspace=tmp_path / "workspace",
        mime_type="application/msword",
        source_path="source.doc",
    )

    assert prepared.legacy_conversion == "libreoffice"
    assert normalized_calls == [converted]


def test_legacy_doc_converter_failure_is_not_source_corruption(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "source.doc"
    source.write_bytes(
        bytes.fromhex("d0cf11e0a1b11ae1")
        + b"\x00" * 504
        + "WordDocument".encode("utf-16-le")
    )
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_to_docx",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConverterCommandError("LibreOffice produced an invalid DOCX")
        ),
    )

    with pytest.raises(ConverterCommandError, match="invalid DOCX"):
        prepare_extraction(
            source,
            workspace=tmp_path / "workspace",
            mime_type="application/msword",
            source_path="source.doc",
        )


def test_google_doc_converter_deletes_temporary_drive_file(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "source.doc"
    source.write_bytes(b"legacy doc")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")
    deleted: list[str] = []

    class Request:
        def __init__(self, result=None, callback=None):  # noqa: ANN001
            self.result = result
            self.callback = callback

        def execute(self):
            if self.callback:
                self.callback()
            return self.result

    class Files:
        def create(self, **_kwargs):
            return Request({"id": "temporary-id"})

        def export_media(self, **_kwargs):
            return object()

        def delete(self, *, fileId):  # noqa: N803, ANN001
            return Request(callback=lambda: deleted.append(fileId))

    class Downloader:
        def __init__(self, handle, _request):  # noqa: ANN001
            self.handle = handle

        def next_chunk(self):
            self.handle.write(payload.getvalue())
            return None, True

    monkeypatch.setattr(
        "app.modules.library.google_doc_conversion.MediaFileUpload",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        "app.modules.library.google_doc_conversion.MediaIoBaseDownload", Downloader
    )
    converter = GoogleDriveDocxConverter()
    converter._service = type("Service", (), {"files": lambda self: Files()})()

    output = converter(
        source,
        workspace=tmp_path / "workspace",
        detected_format="doc",
    )

    assert zipfile.is_zipfile(output)
    assert deleted == ["temporary-id"]


def test_ole_detection_does_not_treat_thumbnail_cache_as_word_document(
    tmp_path: Path,
) -> None:
    ole_header = bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 504
    thumbnail_cache = tmp_path / "Thumbs.db"
    thumbnail_cache.write_bytes(
        ole_header + "256_a43e39b83acfaad6".encode("utf-16-le")
    )
    word = tmp_path / "mislabelled"
    word.write_bytes(ole_header + "WordDocument".encode("utf-16-le"))

    assert detect_document_format(
        thumbnail_cache,
        mime_type="application/cdfv2",
        source_path="Thumbs.db",
    ) == "compound"
    assert detect_document_format(
        word,
        mime_type="application/cdfv2",
        source_path="no-extension",
    ) == "doc"


def test_docx_preserves_math_table_figure_and_public_image_url(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "source.png"
    Image.new("RGB", (16, 12), (150, 20, 20)).save(image_path)
    source = """# Demo

Inline $x^2 + y^2 = z^2$.

| A | B |
|---|---|
| 1 | two |

![A useful caption](source.png)
"""
    docx = tmp_path / "book.docx"
    subprocess.run(
        [
            "pandoc", "-f", "markdown", "-t", "docx", "--resource-path",
            str(tmp_path), "-o", str(docx),
        ],
        input=source,
        text=True,
        check=True,
    )
    wrongly_named = tmp_path / "cached.doc"
    wrongly_named.write_bytes(docx.read_bytes())

    prepared = prepare_extraction(
        wrongly_named,
        workspace=tmp_path / "workspace",
        mime_type="application/msword",
        source_path="incorrect.doc",
    )
    assert prepared.detected_format == "docx"
    assert len(prepared.assets) == 1
    markdown = render_markdown(
        prepared,
        asset_urls={prepared.assets[0].source_ref: "https://s3.example/images/a/1.png"},
    )

    assert "$x^{2} + y^{2} = z^{2}$" in markdown
    assert "<table>" in markdown
    assert '<figure style="text-align: center; margin: 1em 0;">' in markdown
    assert 'src="https://s3.example/images/a/1.png"' in markdown
    assert "<figcaption>A useful caption</figcaption>" in markdown
    assert "```{=html}" not in markdown
    assert (tmp_path / "workspace" / "raw-ast.json").is_file()
    assert (tmp_path / "workspace" / "final.md").is_file()
    report = validate_rendered_markdown(
        prepared,
        markdown,
        asset_urls={prepared.assets[0].source_ref: "https://s3.example/images/a/1.png"},
    )
    assert report["passed"] is True
    assert report["referenced_asset_count"] == 1


def test_content_archive_contains_exact_md5_markdown_member(tmp_path: Path) -> None:
    md5 = "a" * 32
    archive_path = _write_content_archive(
        md5, "# Content\n", tmp_path / f"{md5}.zip"
    )

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [f"{md5}.md"]
        assert archive.read(f"{md5}.md") == b"# Content\n"
        assert archive.getinfo(f"{md5}.md").date_time == (1980, 1, 1, 0, 0, 0)


def test_fb2_embedded_image_enters_common_asset_pipeline(tmp_path: Path) -> None:
    raw_image = tmp_path / "image.png"
    Image.new("RGB", (3, 3), "blue").save(raw_image)
    import base64

    payload = base64.b64encode(raw_image.read_bytes()).decode("ascii")
    fb2 = tmp_path / "book.fb2"
    fb2.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns:l="http://www.w3.org/1999/xlink">
  <body><section><title><p>Title</p></title><p>Text</p>
  <image l:href="#cover"/></section></body>
  <binary id="cover" content-type="image/png">%s</binary>
</FictionBook>""" % payload,
        encoding="utf-8",
    )

    prepared = prepare_extraction(
        fb2,
        workspace=tmp_path / "workspace-fb2",
        mime_type="text/xml",
        source_path="book.fb2",
    )

    assert prepared.detected_format == "fb2"
    assert len(prepared.assets) == 1
    assert json.loads(
        (tmp_path / "workspace-fb2" / "detection.json").read_text()
    )["detected_format"] == "fb2"
    markdown = render_markdown(
        prepared,
        asset_urls={
            prepared.assets[0].source_ref: "https://public.example/cover.png"
        },
    )
    assert markdown.count("Title") == 1


def test_unsupported_embedded_media_is_dropped_without_failing_document(
    tmp_path: Path, monkeypatch,
) -> None:
    media = tmp_path / "media.bin"
    media.write_bytes(b"unsupported proprietary object")
    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {"t": "Para", "c": [{"t": "Str", "c": "Readable text"}]},
            {
                "t": "Para",
                "c": [
                    {
                        "t": "Image",
                        "c": [["", [], []], [{"t": "Str", "c": "Object"}], [str(media), ""]],
                    }
                ],
            },
        ],
    }

    def fail_conversion(*_args, **_kwargs):
        raise RuntimeError("unsupported image encoding")

    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._browser_image", fail_conversion
    )
    assets = _collect_assets(ast, workspace=tmp_path)
    prepared = PreparedExtraction("docx", tmp_path, ast, None, assets)
    markdown = render_markdown(prepared, asset_urls={})

    assert assets == ()
    assert "Readable text" in markdown
    assert str(media) not in markdown
    drops = json.loads((tmp_path / "dropped-media.json").read_text())
    assert drops[0]["source_ref"] == str(media)
    assert "unsupported image encoding" in drops[0]["reason"]


def test_consecutive_and_grouped_images_all_render_as_html_figures(
    tmp_path: Path,
) -> None:
    def image(url: str) -> dict:
        return {
            "t": "Image",
            "c": [["", [], []], [], [url, ""]],
        }

    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {"t": "Para", "c": [image("local-1.png")]},
            {"t": "Para", "c": [image("local-2.png")]},
            {
                "t": "Para",
                "c": [image("local-3.png"), {"t": "Space"}, image("local-4.png")],
            },
            {
                "t": "Div",
                "c": [
                    ["container", [], []],
                    [{"t": "Para", "c": [image("local-5.png")]}],
                ],
            },
            {
                "t": "Para",
                "c": [
                    {
                        "t": "Span",
                        "c": [
                            ["page-anchor", ["style"], []],
                            [{"t": "Str", "c": "Body text"}],
                        ],
                    }
                ],
            },
        ],
    }
    prepared = PreparedExtraction("docx", tmp_path, ast, None, ())
    urls = {
        f"local-{index}.png": f"https://public.example/{index}.png"
        for index in range(1, 6)
    }

    markdown = render_markdown(prepared, asset_urls=urls)

    assert markdown.count("<figure ") == 4
    assert markdown.count("<img ") == 5
    assert "![" not in markdown
    assert ":::" not in markdown
    assert "{.style}" not in markdown
    for url in urls.values():
        assert url in markdown
    assert "Body text" in markdown
    assert validate_rendered_markdown(
        prepared, markdown, asset_urls=urls
    )["passed"] is True


def test_images_mixed_with_text_are_split_into_html_figures(tmp_path: Path) -> None:
    def image(url: str) -> dict:
        return {"t": "Image", "c": [["", [], []], [], [url, ""]]}

    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {
                "t": "Para",
                "c": [
                    {
                        "t": "Underline",
                        "c": [{"t": "Str", "c": "Before"}],
                    },
                    {"t": "Space"},
                    image("local-1.png"),
                    {"t": "Space"},
                    image("local-2.png"),
                    {"t": "Space"},
                    {"t": "Str", "c": "After"},
                ],
            }
        ],
    }
    prepared = PreparedExtraction("rtf", tmp_path, ast, None, ())
    urls = {
        "local-1.png": "https://public.example/1.png",
        "local-2.png": "https://public.example/2.png",
    }

    markdown = render_markdown(prepared, asset_urls=urls)

    assert "Before" in markdown
    assert "After" in markdown
    assert markdown.count("<figure ") == 2
    assert markdown.count("<img ") == 2
    assert "![" not in markdown
    assert "{.underline}" not in markdown


def test_epub_local_document_links_become_plain_text(tmp_path: Path) -> None:
    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {
                "t": "Para",
                "c": [
                    {
                        "t": "Link",
                        "c": [
                            ["", [], []],
                            [{"t": "Str", "c": "Chapter one"}],
                            ["chapter.xhtml#start", ""],
                        ],
                    },
                    {"t": "Space"},
                    {
                        "t": "Link",
                        "c": [
                            ["", [], []],
                            [{"t": "Str", "c": "Publisher"}],
                            ["https://example.com/book", ""],
                        ],
                    },
                ],
            }
        ],
    }
    prepared = PreparedExtraction("epub", tmp_path, ast, None, ())

    markdown = render_markdown(prepared, asset_urls={})

    assert "Chapter one" in markdown
    assert "chapter.xhtml" not in markdown
    assert "[Publisher](https://example.com/book)" in markdown


def test_epub_heading_source_attributes_are_removed(tmp_path: Path) -> None:
    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {
                "t": "Header",
                "c": [
                    1,
                    ["chapter.xhtml#p1", ["coverTtl"], [["pid", "1"]]],
                    [{"t": "Str", "c": "Chapter"}],
                ],
            }
        ],
    }
    prepared = PreparedExtraction("epub", tmp_path, ast, None, ())

    markdown = render_markdown(prepared, asset_urls={})

    assert markdown == "# Chapter\n"


def test_text_decoder_selects_cp866_for_dos_cyrillic() -> None:
    payload = "Program Demo; {Создаем новый тип данных}".encode("cp866")

    assert _decode_text(payload) == "Program Demo; {Создаем новый тип данных}"


def test_text_decoder_preserves_bom_marked_utf16() -> None:
    payload = "Татарча текст".encode("utf-16")

    assert _decode_text(payload) == "Татарча текст"


def test_legacy_markdown_images_enter_backblaze_asset_pipeline(
    tmp_path: Path, monkeypatch,
) -> None:
    first = "https://storage.yandexcloud.net/ttimg/legacy-1.jpg"
    second = "https://storage.yandexcloud.net/ttimg/legacy-2.jpg"
    source = tmp_path / "book.md"
    source.write_text(
        f"# Book\n\n![Cover]({first})\n\n"
        f'<figure><img alt="Map" src="{second}"></figure>\n',
        encoding="utf-8",
    )

    def fake_download(url: str, *, workspace: Path, ordinal: int) -> Path:
        assert url in {first, second}
        path = workspace / "remote-media" / f"{ordinal}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (3, 3), "green").save(path)
        return path

    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._download_legacy_image",
        fake_download,
    )
    prepared = prepare_extraction(
        source,
        workspace=tmp_path / "workspace-markdown",
        mime_type="text/plain",
        source_path="book.md",
    )
    urls = {
        prepared.assets[0].source_ref: "https://backblaze.example/1.jpg",
        prepared.assets[1].source_ref: "https://backblaze.example/2.jpg",
    }

    markdown = render_markdown(prepared, asset_urls=urls)

    assert len(prepared.assets) == 2
    assert first not in markdown
    assert second not in markdown
    assert markdown.count("<img ") == 2
    assert "https://backblaze.example/1.jpg" in markdown
    assert "https://backblaze.example/2.jpg" in markdown
    assert validate_rendered_markdown(
        prepared, markdown, asset_urls=urls
    )["passed"] is True


def test_validation_rejects_unmanaged_external_html_image(tmp_path: Path) -> None:
    prepared = PreparedExtraction("markdown", tmp_path, None, "Text\n", ())

    with pytest.raises(ValueError, match="unmanaged HTML image"):
        validate_rendered_markdown(
            prepared,
            '<figure><img src="https://external.example/image.jpg"></figure>\n',
            asset_urls={},
        )


def test_image_only_document_is_rejected_as_requiring_ocr(tmp_path: Path) -> None:
    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {
                "t": "Para",
                "c": [
                    {
                        "t": "Image",
                        "c": [
                            ["", [], []],
                            [],
                            ["page.jpeg", ""],
                        ],
                    }
                ],
            }
        ],
    }
    prepared = PreparedExtraction("doc", tmp_path, ast, None, ())

    with pytest.raises(ValueError, match="OCR required"):
        render_markdown(
            prepared,
            asset_urls={"page.jpeg": "https://public.example/page.jpeg"},
        )


class _AssetS3:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def list_objects_v2(self, **_request):
        return {
            "Contents": [
                {"Key": "a" * 32 + "/1.png"},
                {"Key": "a" * 32 + "/2.png"},
                {"Key": "a" * 32 + "/old.png"},
            ],
            "IsTruncated": False,
        }

    def delete_object(self, *, Bucket, Key):  # noqa: ANN001, N803
        del Bucket
        self.deleted.append(Key)


def test_stale_image_cleanup_keeps_only_current_manifest() -> None:
    md5 = "a" * 32
    s3 = _AssetS3()

    deleted = _delete_stale_assets(
        s3,
        bucket="images",
        md5=md5,
        expected_keys={f"{md5}/1.png", f"{md5}/2.png"},
    )

    assert deleted == 1
    assert s3.deleted == [f"{md5}/old.png"]


def test_publication_validation_rejects_an_unreferenced_asset(tmp_path: Path) -> None:
    asset = tmp_path / "image.png"
    Image.new("RGB", (2, 2), "red").save(asset)
    prepared = PreparedExtraction(
        "docx",
        tmp_path,
        None,
        "Text\n",
        (ExtractedAsset("local.png", asset, 1),),
    )

    with pytest.raises(ValueError, match="not rendered as an HTML image"):
        validate_rendered_markdown(
            prepared,
            "Text\n",
            asset_urls={"local.png": "https://public.example/1.png"},
        )

    report = json.loads((tmp_path / "validation.json").read_text())
    assert report["passed"] is False


def test_converter_timeout_terminates_descendant_process_group(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="timed out"):
        _run(
            ["bash", "-c", "sleep 60 & child=$!; echo $child; wait"],
            workspace=tmp_path,
            label="timeout",
            timeout_seconds=1,
        )

    child_pid = int((tmp_path / "timeout.stdout.log").read_text().strip())
    for _attempt in range(20):
        if not Path(f"/proc/{child_pid}").exists():
            break
        time.sleep(0.05)
    assert not Path(f"/proc/{child_pid}").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


class _Rows:
    def __init__(self, rows) -> None:  # noqa: ANN001
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Connection:
    def __init__(self, engine) -> None:  # noqa: ANN001
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, params):  # noqa: ANN001
        self.engine.sql = str(statement)
        self.engine.params = dict(params)
        return _Rows(self.engine.rows)


class _Engine:
    def __init__(self, rows=None) -> None:  # noqa: ANN001
        self.rows = list(rows or [])
        self.sql = ""
        self.params = {}

    def connect(self):
        return _Connection(self)

    def begin(self):
        return _Connection(self)


def test_candidate_queue_backfills_legacy_content_and_versions_unsupported() -> None:
    repository = NonPdfExtractionRepository.__new__(NonPdfExtractionRepository)
    repository.engine = _Engine()

    repository.list_candidates(
        extractor_version="nonpdf.v7", limit=10, per_mime_limit=100
    )

    assert "AND d.content_url IS NULL" not in repository.engine.sql
    assert "state.extractor_version IS DISTINCT FROM" in repository.engine.sql
    assert "state.status = 'processing'" in repository.engine.sql
    assert "state.status = 'failed'" in repository.engine.sql
    assert "state.attempt_count < :max_automatic_attempts" in repository.engine.sql
    assert "state.status = 'deferred'" in repository.engine.sql
    assert "primary_storage_verified_at IS NOT NULL" in repository.engine.sql
    assert "WHEN state.md5 IS NULL THEN 0" in repository.engine.sql
    assert "WHEN state.status = 'failed' THEN 3" in repository.engine.sql
    assert "CASE WHEN d.content_url IS NULL THEN 0 ELSE 1 END" in repository.engine.sql
    assert "ROW_NUMBER() OVER" in repository.engine.sql
    assert "d.mime_rank <=" in repository.engine.sql
    assert repository.engine.params["per_mime_limit"] == 100
    assert repository.engine.params["max_automatic_attempts"] == 3
    assert repository.engine.params["retry_known_failures"] is False


def test_candidate_queue_can_explicitly_retry_known_failures() -> None:
    repository = NonPdfExtractionRepository.__new__(NonPdfExtractionRepository)
    repository.engine = _Engine()

    repository.list_candidates(
        extractor_version="nonpdf.v7", retry_known_failures=True
    )

    assert repository.engine.params["retry_known_failures"] is True
    assert "cleanup.reason = 'corrupted'" in repository.engine.sql


class _CorruptRuntimeRepository:
    def __init__(self, candidate: NonPdfCandidate) -> None:
        self.candidate = candidate
        self.outcomes: list[dict] = []

    def list_candidates(self, **_kwargs):
        return [self.candidate]

    def start_attempt(self, *_args, **_kwargs):
        return None

    def mark_outcome(self, _md5, **kwargs):  # noqa: ANN001
        self.outcomes.append(dict(kwargs))


class _CorruptCleanupRepository:
    def __init__(self) -> None:
        self.plans: list[dict] = []

    def enqueue_cleanup(self, payload):  # noqa: ANN001
        self.plans.append(dict(payload))
        return len(self.plans), True


class _ProgressDb:
    def update_run_progress(self, *_args, **_kwargs):
        return None

    def insert_event(self, *_args, **_kwargs):
        return None


def test_non_pdf_runtime_queues_structural_corruption(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"broken")
    candidate = NonPdfCandidate(
        md5="a" * 32,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        source_path="/documents/nested/source.docx",
        document_url="https://s3.example/source.docx",
        primary_storage_size=6,
        content_url=None,
    )
    repository = _CorruptRuntimeRepository(candidate)
    cleanup = _CorruptCleanupRepository()
    storage = type(
        "Storage",
        (),
        {
            "cache_path": tmp_path / "cache",
            "source_path": "/documents",
            "filtered_out_path": "/filtered",
            "content_bucket": "content",
            "content_images_bucket": "images",
        },
    )()
    monkeypatch.setattr(
        "app.modules.library.runtime.run_extract_non_pdf.download_cached_primary_document",
        lambda **_kwargs: source,
    )
    monkeypatch.setattr(
        "app.modules.library.runtime.run_extract_non_pdf.prepare_extraction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CorruptDocumentError("document_container", "invalid ZIP")
        ),
    )

    summary = run_extraction(
        repository=repository,
        cleanup_repository=cleanup,
        db=_ProgressDb(),
        s3=object(),
        storage=storage,
        workspace=tmp_path / "run",
        run_id=71,
        should_stop=lambda: False,
    )

    assert summary["corrupted"] == 1
    assert cleanup.plans[0]["target_path"] == (
        "/filtered/corrupted/nested/source.docx"
    )
    assert cleanup.plans[0]["evidence"]["detector"] == "document_container"
    assert repository.outcomes[0]["status"] == "failed"


def test_new_extractor_version_resets_automatic_attempt_budget() -> None:
    repository = NonPdfExtractionRepository.__new__(NonPdfExtractionRepository)
    repository.engine = _Engine()

    repository.start_attempt("a" * 32, extractor_version="nonpdf.v8", run_id=9)

    assert "IS DISTINCT FROM EXCLUDED.extractor_version" in repository.engine.sql
    assert "THEN 1" in repository.engine.sql


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValueError("Extracted document contains only images; OCR required"), "deferred"),
        (ValueError("Rendered Markdown validation failed: bad image"), "deferred"),
        (RuntimeError("LibreOffice produced 0 DOCX files"), "deferred"),
        (
            RuntimeError(
                "pandoc-read failed: couldn't unpack docx container: "
                "Content size mismatch"
            ),
            "deferred",
        ),
        (RuntimeError("libreoffice timed out after 900 seconds"), "failed"),
        (RuntimeError("temporary S3 failure"), "failed"),
    ],
)
def test_failure_status_separates_deterministic_and_retryable_errors(
    error: Exception, expected: str
) -> None:
    assert _failure_status(error) == expected
