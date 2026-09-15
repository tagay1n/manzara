"""Library non pdf converters coverage."""

from __future__ import annotations

import io
import os
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from app.modules.library.google_doc_conversion import GoogleDriveDocxConverter
from app.modules.library.non_pdf_converters import _run
from app.modules.library.non_pdf_extraction import (
    ConverterCommandError,
    prepare_extraction,
)


def test_legacy_doc_uses_google_fallback_for_invalid_libreoffice_output(
    tmp_path: Path,
    monkeypatch,
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
    tmp_path: Path,
    monkeypatch,
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
    tmp_path: Path,
    monkeypatch,
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
    tmp_path: Path,
    monkeypatch,
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
