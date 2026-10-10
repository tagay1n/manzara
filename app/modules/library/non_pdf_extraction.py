"""Non-PDF extraction orchestration and public API."""

from __future__ import annotations

import json
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Callable
from xml.etree import ElementTree

from app.modules.library.corrupt_document import CorruptDocumentError
from app.modules.library.non_pdf_book_converters import (
    _convert_mobi_to_epub,
    _convert_odt_to_html,
    _validate_converted_epub,
)
from app.modules.library.non_pdf_large_docx import maybe_convert_large_docx_to_html
from app.modules.library.non_pdf_html_source import prepare_html_source
from app.modules.library.non_pdf_converters import (
    _convert_to_docx,
    _convert_to_pptx,
    _convert_spreadsheet_to_html,
    _fb2_to_html,
    _normalize_docx_archive,
    _run,
    _validate_converted_docx,
    _validate_converted_pptx,
    require_converter_binaries,
)
from app.modules.library.non_pdf_formats import (
    _SUPPORTED_FORMATS,
    _decode_text,
    DetectedDocumentFormat,
    inspect_document_format,
    validate_source_archive,
)
from app.modules.library.non_pdf_pptx import pptx_to_html
from app.modules.library.non_pdf_spreadsheet import select_spreadsheet_blocks
from app.modules.library.non_pdf_media import _collect_assets
from app.modules.library.non_pdf_rendering import (
    render_markdown,
    validate_rendered_markdown,
)
from app.modules.library.non_pdf_types import (
    EXTRACTOR_VERSION,
    ConverterCommandError,
    ConverterTimeoutError,
    PreparedExtraction,
    DeferredDocumentExtraction,
    UnsupportedDocumentFormat,
)


def prepare_extraction(
    source: Path,
    *,
    workspace: Path,
    mime_type: str,
    source_path: str,
    legacy_doc_converter: Callable[..., Path] | None = None,
    legacy_presentation_converter: Callable[..., Path] | None = None,
    on_detected: Callable[[DetectedDocumentFormat], None] | None = None,
) -> PreparedExtraction:
    workspace.mkdir(parents=True, exist_ok=True)
    if source.stat().st_size <= 0:
        raise CorruptDocumentError("empty_source", "Source document is empty")
    try:
        inspection = inspect_document_format(source, mime_type=mime_type, source_path=source_path)
        detected = inspection.format
    except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
        raise CorruptDocumentError("document_container", str(exc)) from exc
    source_name = PurePosixPath(str(source_path or source.name)).name
    if detected == "doc" and source_name.startswith("~$"):
        raise CorruptDocumentError(
            "temporary_source",
            "Word owner/lock file is not a document",
        )
    validate_source_archive(source, detected)
    (workspace / "detection.json").write_text(
        json.dumps(
            {
                "detected_format": detected,
                "verified_mime_type": inspection.verified_mime_type,
                "catalog_mime_type": mime_type,
                "source_path": source_path,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if on_detected is not None:
        on_detected(inspection)
    if detected not in _SUPPORTED_FORMATS:
        raise UnsupportedDocumentFormat(detected)
    if detected in {"markdown", "text"}:
        try:
            text_value = _decode_text(source.read_bytes())
        except ValueError as exc:
            raise CorruptDocumentError("text_decode", str(exc)) from exc
        if detected == "text" and not text_value.endswith("\n"):
            text_value += "\n"
        return PreparedExtraction(detected, workspace, None, text_value, ())

    pandoc_source = source
    pandoc_format = detected
    legacy_conversion: str | None = None
    if detected == "html":
        pandoc_source = prepare_html_source(source, workspace=workspace)
    elif detected in {"doc", "rtf"}:
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
    elif detected == "pptx":
        pandoc_source = pptx_to_html(source, workspace=workspace)
        pandoc_format = "html"
    elif detected == "powerpoint":
        pandoc_source, legacy_conversion = _prepare_legacy_powerpoint(
            source,
            workspace=workspace,
            fallback=legacy_presentation_converter,
        )
        pandoc_format = "html"
    elif detected == "fb2":
        try:
            pandoc_source = _fb2_to_html(source, workspace=workspace)
        except (ElementTree.ParseError, ValueError) as exc:
            raise CorruptDocumentError("document_parse", str(exc)) from exc
        pandoc_format = "html"
    elif detected == "spreadsheet":
        pandoc_source = _convert_spreadsheet_to_html(source, workspace=workspace)
        pandoc_format = "html"
    elif detected == "odt":
        pandoc_source = _convert_odt_to_html(source, workspace=workspace)
        pandoc_format = "html"
        legacy_conversion = "libreoffice"
    elif detected == "mobi":
        pandoc_source = _convert_mobi_to_epub(source, workspace=workspace)
        _validate_converted_epub(pandoc_source)
        pandoc_format = "epub"
        legacy_conversion = "calibre"
    if detected in {"doc", "docx"}:
        large_html = maybe_convert_large_docx_to_html(pandoc_source, workspace=workspace)
        if large_html is not None:
            pandoc_source, pandoc_format = large_html, "html"
    resource_base = (
        pandoc_source.parent
        if pandoc_format == "html" and detected in {"doc", "docx", "odt", "html"}
        else None
    )
    ast_path = workspace / "raw-ast.json"
    media_dir = workspace / "media"
    try:
        _run(
            [
                "pandoc",
                str(pandoc_source),
                "-f",
                pandoc_format,
                "-t",
                "json",
                "--extract-media",
                str(media_dir),
                *([f"--resource-path={resource_base}"] if resource_base else []),
                "-o",
                str(ast_path),
            ],
            workspace=workspace,
            label="pandoc-read",
        )
    except ConverterCommandError as exc:
        if detected in {"doc", "rtf", "spreadsheet", "odt", "mobi"}:
            raise ConverterCommandError(
                f"Converted {detected} document failed Pandoc parsing: {exc}"
            ) from exc
        raise CorruptDocumentError("document_parse", str(exc)) from exc
    ast = json.loads(ast_path.read_text(encoding="utf-8"))
    if detected == "spreadsheet":
        select_spreadsheet_blocks(ast)
    # Only body blocks are rendered into the published Markdown. Avoid uploading
    # images that occur solely in Pandoc metadata and can never be referenced.
    assets = _collect_assets(
        {"blocks": ast.get("blocks", [])},
        workspace=workspace,
        source_base=resource_base,
    )
    return PreparedExtraction(
        detected, workspace, ast, None, assets, legacy_conversion=legacy_conversion
    )


def _prepare_legacy_powerpoint(
    source: Path,
    *,
    workspace: Path,
    fallback: Callable[..., Path] | None,
) -> tuple[Path, str]:
    try:
        converted = _convert_to_pptx(source, workspace=workspace)
        _validate_converted_pptx(converted)
        try:
            return pptx_to_html(converted, workspace=workspace, strict_layout=True), "libreoffice"
        except DeferredDocumentExtraction as exc:
            if exc.reason != "pptx_no_text":
                raise
            if fallback is None:
                raise
        except CorruptDocumentError as exc:
            raise ConverterCommandError(
                f"Converted PowerPoint could not be parsed: {exc}"
            ) from exc
    except (ConverterCommandError, ConverterTimeoutError):
        if fallback is None:
            raise
    converted = fallback(source, workspace=workspace)
    _validate_converted_pptx(converted)
    try:
        return pptx_to_html(converted, workspace=workspace, strict_layout=True), "google_drive"
    except CorruptDocumentError as exc:
        raise ConverterCommandError(
            f"Google-converted PowerPoint could not be parsed: {exc}"
        ) from exc

__all__ = [
    "EXTRACTOR_VERSION",
    "PreparedExtraction",
    "UnsupportedDocumentFormat",
    "prepare_extraction",
    "render_markdown",
    "require_converter_binaries",
    "validate_rendered_markdown",
]
