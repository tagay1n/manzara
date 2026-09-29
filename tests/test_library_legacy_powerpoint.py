"""Legacy PowerPoint conversion and conservative publication checks."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from app.modules.library.non_pdf_extraction import prepare_extraction, render_markdown
from app.modules.library.non_pdf_converters import _convert_to_pptx
from app.modules.library.non_pdf_formats import detect_document_format
from app.modules.library.non_pdf_types import DeferredDocumentExtraction


def _binary_powerpoint(path: Path) -> Path:
    path.write_bytes(
        bytes.fromhex("d0cf11e0a1b11ae1")
        + b"\x00" * 504
        + "PowerPoint Document".encode("utf-16-le")
    )
    return path


def _pptx(path: Path, *, extra_shape: str = "", body: str = "Tatar text") -> Path:
    presentation = (
        '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<p:sldIdLst><p:sldId id="256" r:id="r1"/></p:sldIdLst></p:presentation>'
    )
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
        'Target="slides/slide1.xml"/></Relationships>'
    )
    slide = (
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>'
        + body
        + '</a:t></a:r></a:p></p:txBody></p:sp>'
        + extra_shape
        + '</p:spTree></p:cSld></p:sld>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", relationships)
        archive.writestr("ppt/slides/slide1.xml", slide)
    return path


def test_powerpoint_requires_binary_container_and_stream_marker(tmp_path: Path) -> None:
    unverified_ole = tmp_path / "unverified.ppt"
    unverified_ole.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\0" * 512)
    assert detect_document_format(unverified_ole, source_path=unverified_ole.name) == "compound"

    malformed_pptx = tmp_path / "malformed.pptx"
    with zipfile.ZipFile(malformed_pptx, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", "<slide/>")
    assert detect_document_format(malformed_pptx, source_path=malformed_pptx.name) == "pptx"


def test_legacy_powerpoint_uses_local_conversion_and_keeps_slide_boundary(
    monkeypatch, tmp_path: Path
) -> None:
    source = _binary_powerpoint(tmp_path / "mislabelled.pptx")
    converted = _pptx(tmp_path / "converted.pptx")
    calls = []

    def convert(path, *, workspace):
        calls.append((path, workspace))
        return converted

    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_to_pptx", convert
    )
    prepared = prepare_extraction(
        source,
        workspace=tmp_path / "work",
        mime_type="application/vnd.ms-powerpoint",
        source_path="mislabelled.pptx",
    )

    assert prepared.detected_format == "powerpoint"
    assert prepared.legacy_conversion == "libreoffice"
    assert calls == [(source, tmp_path / "work")]
    assert "# Slide 1" in render_markdown(prepared, asset_urls={})


def test_local_conversion_stages_binary_ppt_and_isolates_libreoffice(
    monkeypatch, tmp_path: Path
) -> None:
    source = _binary_powerpoint(tmp_path / "wrong.pptx")
    commands = []

    def run(command, *, workspace, label, timeout_seconds):
        commands.append((command, workspace, label, timeout_seconds))
        _pptx(workspace / "converted/source.pptx")

    monkeypatch.setattr("app.modules.library.non_pdf_converters._run", run)
    workspace = tmp_path / "work"
    converted = _convert_to_pptx(source, workspace=workspace)

    assert converted.is_file()
    assert (workspace / "source.ppt").read_bytes() == source.read_bytes()
    command, called_workspace, label, timeout = commands[0]
    assert command[-1] == str(workspace / "source.ppt")
    assert command[2:4] == ["--headless", "--convert-to"]
    assert command[1].startswith("-env:UserInstallation=file:")
    assert called_workspace == workspace and label == "libreoffice" and timeout == 900


def test_legacy_powerpoint_uses_google_after_invalid_local_output(
    monkeypatch, tmp_path: Path
) -> None:
    source = _binary_powerpoint(tmp_path / "source.ppt")
    invalid = tmp_path / "invalid.pptx"
    with zipfile.ZipFile(invalid, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    recovered = _pptx(tmp_path / "google.pptx")
    calls = []
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_to_pptx",
        lambda *_args, **_kwargs: invalid,
    )

    def google(path, *, workspace):
        calls.append(path)
        return recovered

    prepared = prepare_extraction(
        source,
        workspace=tmp_path / "work",
        mime_type="application/vnd.ms-powerpoint",
        source_path="source.ppt",
        legacy_presentation_converter=google,
    )
    assert prepared.legacy_conversion == "google_drive"
    assert calls == [source]


def test_legacy_powerpoint_uses_google_after_local_native_text_is_empty(
    monkeypatch, tmp_path: Path
) -> None:
    source = _binary_powerpoint(tmp_path / "source.ppt")
    local = _pptx(tmp_path / "empty.pptx", body="")
    recovered = _pptx(tmp_path / "google.pptx")
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_to_pptx",
        lambda *_args, **_kwargs: local,
    )
    prepared = prepare_extraction(
        source,
        workspace=tmp_path / "work",
        mime_type="application/vnd.ms-powerpoint",
        source_path="source.ppt",
        legacy_presentation_converter=lambda *_args, **_kwargs: recovered,
    )
    assert prepared.legacy_conversion == "google_drive"


def test_legacy_powerpoint_defers_connectors_and_retains_inspection(
    monkeypatch, tmp_path: Path
) -> None:
    source = _binary_powerpoint(tmp_path / "source.ppt")
    converted = _pptx(tmp_path / "converted.pptx", extra_shape="<p:cxnSp/>")
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_to_pptx",
        lambda *_args, **_kwargs: converted,
    )
    workspace = tmp_path / "work"
    with pytest.raises(DeferredDocumentExtraction) as caught:
        prepare_extraction(
            source,
            workspace=workspace,
            mime_type="application/vnd.ms-powerpoint",
            source_path="source.ppt",
        )
    assert caught.value.detected_format == "powerpoint"
    assert caught.value.reason == "pptx_ambiguous_layout"
    report = json.loads((workspace / "pptx-inspection.json").read_text())
    assert "pptx_ambiguous_layout" in report["reasons"]


def test_legacy_powerpoint_defers_side_by_side_text_boxes(
    monkeypatch, tmp_path: Path
) -> None:
    source = _binary_powerpoint(tmp_path / "source.ppt")
    left = (
        '<p:sp><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/>'
        '</a:xfrm></p:spPr><p:txBody><a:p><a:r><a:t>Left</a:t></a:r></a:p>'
        '</p:txBody></p:sp>'
    )
    right = left.replace('x="0"', 'x="200"').replace("Left", "Right")
    converted = _pptx(tmp_path / "converted.pptx", extra_shape=left + right)
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_to_pptx",
        lambda *_args, **_kwargs: converted,
    )
    with pytest.raises(DeferredDocumentExtraction, match="pptx_ambiguous_layout"):
        prepare_extraction(
            source,
            workspace=tmp_path / "work",
            mime_type="application/vnd.ms-powerpoint",
            source_path="source.ppt",
        )


def test_legacy_powerpoint_does_not_send_visual_deferral_to_google(
    monkeypatch, tmp_path: Path
) -> None:
    source = _binary_powerpoint(tmp_path / "source.ppt")
    converted = _pptx(tmp_path / "converted.pptx", extra_shape="<p:pic/>")
    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._convert_to_pptx",
        lambda *_args, **_kwargs: converted,
    )
    with pytest.raises(DeferredDocumentExtraction, match="pptx_slide_images"):
        prepare_extraction(
            source,
            workspace=tmp_path / "work",
            mime_type="application/vnd.ms-powerpoint",
            source_path="source.ppt",
            legacy_presentation_converter=lambda *_args, **_kwargs: pytest.fail(
                "visual deferral must not trigger Google fallback"
            ),
        )


def test_google_presentation_converter_cleans_up_uploaded_file(
    monkeypatch, tmp_path: Path
) -> None:
    from app.modules.library.google_presentation_conversion import (
        GoogleDrivePptxConverter,
    )

    source = _binary_powerpoint(tmp_path / "source.ppt")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<presentation/>")
    deleted = []
    created = []

    class Request:
        def __init__(self, result=None, callback=None):
            self.result = result
            self.callback = callback

        def execute(self):
            if self.callback:
                self.callback()
            return self.result

    class Files:
        def create(self, **kwargs):
            created.append(kwargs)
            return Request({"id": "temporary-id"})

        def export_media(self, **kwargs):
            created.append(kwargs)
            return object()

        def delete(self, *, fileId):  # noqa: N803
            return Request(callback=lambda: deleted.append(fileId))

    class Downloader:
        def __init__(self, handle, _request):
            self.handle = handle

        def next_chunk(self):
            self.handle.write(payload.getvalue())
            return None, True

    monkeypatch.setattr(
        "app.modules.library.google_presentation_conversion.MediaFileUpload",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        "app.modules.library.google_presentation_conversion.MediaIoBaseDownload",
        Downloader,
    )
    converter = GoogleDrivePptxConverter()
    converter._service = type("Service", (), {"files": lambda self: Files()})()
    output = converter(source, workspace=tmp_path / "work")

    assert output.is_file()
    assert created[0]["body"]["mimeType"] == "application/vnd.google-apps.presentation"
    assert created[1]["mimeType"].endswith("presentationml.presentation")
    assert deleted == ["temporary-id"]


def test_google_presentation_converter_cleans_up_after_export_failure(
    monkeypatch, tmp_path: Path
) -> None:
    from app.modules.library.google_presentation_conversion import (
        GoogleDrivePptxConverter,
    )

    source = _binary_powerpoint(tmp_path / "source.ppt")
    deleted = []

    class Request:
        def __init__(self, result=None):
            self.result = result

        def execute(self):
            return self.result

    class Files:
        def create(self, **_kwargs):
            return Request({"id": "temporary-id"})

        def export_media(self, **_kwargs):
            raise RuntimeError("export unavailable")

        def delete(self, *, fileId):  # noqa: N803
            deleted.append(fileId)
            return Request()

    monkeypatch.setattr(
        "app.modules.library.google_presentation_conversion.MediaFileUpload",
        lambda *_args, **_kwargs: object(),
    )
    converter = GoogleDrivePptxConverter()
    converter._service = type("Service", (), {"files": lambda self: Files()})()

    with pytest.raises(RuntimeError, match="export unavailable"):
        converter(source, workspace=tmp_path / "work")
    assert deleted == ["temporary-id"]
