"""Non-PDF extraction orchestration and public API."""

from __future__ import annotations

import json
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Callable
from xml.etree import ElementTree

from app.modules.library.corrupt_document import CorruptDocumentError
from app.modules.library.non_pdf_converters import (
    _convert_to_docx,
    _fb2_to_html,
    _normalize_docx_archive,
    _run,
    _validate_converted_docx,
    require_converter_binaries,
)
from app.modules.library.non_pdf_formats import (
    _SUPPORTED_FORMATS,
    _decode_text,
    detect_document_format,
)
from app.modules.library.non_pdf_media import _collect_assets, _collect_markdown_assets
from app.modules.library.non_pdf_rendering import (
    render_markdown,
    validate_rendered_markdown,
)
from app.modules.library.non_pdf_types import (
    EXTRACTOR_VERSION,
    ConverterCommandError,
    ConverterTimeoutError,
    ExtractedAsset,
    PreparedExtraction,
    UnsupportedDocumentFormat,
)


def prepare_extraction(
    source: Path,
    *,
    workspace: Path,
    mime_type: str,
    source_path: str,
    legacy_doc_converter: Callable[..., Path] | None = None,
) -> PreparedExtraction:
    workspace.mkdir(parents=True, exist_ok=True)
    if source.stat().st_size <= 0:
        raise CorruptDocumentError("empty_source", "Source document is empty")
    try:
        detected = detect_document_format(
            source, mime_type=mime_type, source_path=source_path
        )
    except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
        raise CorruptDocumentError("document_container", str(exc)) from exc
    source_name = PurePosixPath(str(source_path or source.name)).name
    if detected == "doc" and source_name.startswith("~$"):
        raise CorruptDocumentError(
            "temporary_source",
            "Word owner/lock file is not a document",
        )
    if detected in {"docx", "odt", "epub"}:
        if not zipfile.is_zipfile(source):
            raise CorruptDocumentError(
                "document_container",
                f"Detected {detected} document is not a valid ZIP container",
            )
        try:
            with zipfile.ZipFile(source) as archive:
                if bad_member := archive.testzip():
                    raise CorruptDocumentError(
                        "document_container",
                        f"Corrupt ZIP member: {bad_member}",
                    )
        except CorruptDocumentError:
            raise
        except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
            raise CorruptDocumentError("document_container", str(exc)) from exc
    (workspace / "detection.json").write_text(
        json.dumps(
            {
                "detected_format": detected,
                "catalog_mime_type": mime_type,
                "source_path": source_path,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if detected not in _SUPPORTED_FORMATS:
        raise UnsupportedDocumentFormat(detected)
    if detected in {"markdown", "text"}:
        try:
            text_value = _decode_text(source.read_bytes())
        except ValueError as exc:
            raise CorruptDocumentError("text_decode", str(exc)) from exc
        if detected == "text" and not text_value.endswith("\n"):
            text_value += "\n"
        assets = (
            _collect_markdown_assets(text_value, workspace=workspace)
            if detected == "markdown"
            else ()
        )
        return PreparedExtraction(detected, workspace, None, text_value, assets)

    pandoc_source = Path(source)
    pandoc_format = detected
    legacy_conversion: str | None = None
    if detected in {"doc", "rtf"}:
        try:
            pandoc_source = _convert_to_docx(
                source, workspace=workspace, detected_format=detected
            )
            _validate_converted_docx(pandoc_source)
            pandoc_source = _normalize_docx_archive(
                pandoc_source, workspace=workspace
            )
            _validate_converted_docx(pandoc_source)
            legacy_conversion = "libreoffice"
        except (ConverterCommandError, ConverterTimeoutError):
            if legacy_doc_converter is None:
                raise
            pandoc_source = legacy_doc_converter(
                source,
                workspace=workspace,
                detected_format=detected,
            )
            _validate_converted_docx(pandoc_source)
            legacy_conversion = "google_drive"
        pandoc_format = "docx"
    elif detected == "fb2":
        try:
            pandoc_source = _fb2_to_html(source, workspace=workspace)
        except (ElementTree.ParseError, ValueError) as exc:
            raise CorruptDocumentError("document_parse", str(exc)) from exc
        pandoc_format = "html"
    ast_path = workspace / "raw-ast.json"
    media_dir = workspace / "media"
    try:
        result = _run(
            [
                "pandoc",
                str(pandoc_source),
                "-f",
                pandoc_format,
                "-t",
                "json",
                "--extract-media",
                str(media_dir),
                "-o",
                str(ast_path),
            ],
            workspace=workspace,
            label="pandoc-read",
        )
    except ConverterCommandError as exc:
        if detected in {"doc", "rtf"}:
            raise ConverterCommandError(
                f"Converted legacy document failed Pandoc parsing: {exc}"
            ) from exc
        raise CorruptDocumentError("document_parse", str(exc)) from exc
    del result
    ast = json.loads(ast_path.read_text(encoding="utf-8"))
    # Only body blocks are rendered into the published Markdown. Avoid uploading
    # images that occur solely in Pandoc metadata and can never be referenced.
    assets = _collect_assets(
        {"blocks": ast.get("blocks", [])}, workspace=workspace
    )
    return PreparedExtraction(
        detected,
        workspace,
        ast,
        None,
        assets,
        legacy_conversion=legacy_conversion,
    )


__all__ = [
    "EXTRACTOR_VERSION",
    "ExtractedAsset",
    "PreparedExtraction",
    "UnsupportedDocumentFormat",
    "detect_document_format",
    "prepare_extraction",
    "render_markdown",
    "require_converter_binaries",
    "validate_rendered_markdown",
]
