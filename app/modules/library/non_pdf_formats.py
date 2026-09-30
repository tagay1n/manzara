"""Byte-based document format detection and legacy text decoding."""

from __future__ import annotations

import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import olefile

from app.modules.library.corrupt_document import CorruptDocumentError

_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".xml", ".tex",
    ".srt", ".json", ".yaml", ".yml", ".ini",
}

_HTML_SUFFIXES = {".html", ".htm"}

_SUPPORTED_FORMATS = {
    "doc", "docx", "rtf", "odt", "epub", "fb2", "html", "markdown", "text", "pptx", "powerpoint", "spreadsheet", "mobi"
}


@dataclass(frozen=True)
class DetectedDocumentFormat:
    format: str
    verified_mime_type: str | None = None


def detect_document_format(path: Path, *, mime_type: str = "", source_path: str = "") -> str:
    return inspect_document_format(
        path, mime_type=mime_type, source_path=source_path
    ).format


def inspect_document_format(
    path: Path, *, mime_type: str = "", source_path: str = ""
) -> DetectedDocumentFormat:
    """Classify source bytes before considering unreliable catalog hints."""
    source = Path(path)
    with source.open("rb") as stream:
        header = stream.read(8192)
    lowered = header.lstrip().lower()
    suffix = PurePosixPath(str(source_path or source.name)).suffix.lower()
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if header.startswith(b"%PDF-"):
        return DetectedDocumentFormat("pdf")
    if lowered.startswith(b"{\\rtf"):
        return DetectedDocumentFormat("rtf")
    if header[60:68] == b"BOOKMOBI":
        return DetectedDocumentFormat("mobi")
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            if "word/document.xml" in names:
                return DetectedDocumentFormat("docx")
            if "mimetype" in names:
                value = archive.read("mimetype").decode("ascii", errors="ignore").strip()
                if value == "application/epub+zip":
                    return DetectedDocumentFormat("epub")
                if value == "application/vnd.oasis.opendocument.text":
                    return DetectedDocumentFormat("odt")
            if "META-INF/container.xml" in names:
                return DetectedDocumentFormat("epub")
            if any(name.startswith("ppt/") for name in names):
                return DetectedDocumentFormat("pptx")
            if "xl/workbook.xml" in names and any(
                name.startswith("xl/worksheets/") and name.endswith(".xml")
                for name in names
            ):
                return DetectedDocumentFormat("spreadsheet")
    if header.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        try:
            roots = {name.casefold() for name in _root_ole_streams(source)}
        except (OSError, ValueError, TypeError):
            return DetectedDocumentFormat("compound")
        families = {
            family
            for family, names in {
                "doc": {"worddocument"},
                "powerpoint": {"powerpoint document"},
                "spreadsheet": {"workbook", "book"},
            }.items()
            if roots & names
        }
        if len(families) != 1:
            return DetectedDocumentFormat("compound")
        family = families.pop()
        return DetectedDocumentFormat(
            family,
            {
                "doc": "application/msword",
                "powerpoint": "application/vnd.ms-powerpoint",
            }.get(family),
        )
    sample = lowered[:4096]
    if b"<fictionbook" in sample or suffix == ".fb2" or "fictionbook" in mime:
        return DetectedDocumentFormat("fb2")
    if (
        b"<!doctype html" in sample
        or b"<html" in sample
        or suffix in _HTML_SUFFIXES
        or mime == "text/html"
    ):
        return DetectedDocumentFormat("html")
    if suffix == ".xlsx" or mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        raise zipfile.BadZipFile("XLSX source lacks a ZIP workbook container")
    if suffix == ".epub" or mime == "application/epub+zip":
        return DetectedDocumentFormat("epub")
    if suffix == ".pptx" or mime == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        return DetectedDocumentFormat("pptx")
    if suffix == ".docx" or "wordprocessingml" in mime:
        return DetectedDocumentFormat("docx")
    if suffix == ".odt" or mime == "application/vnd.oasis.opendocument.text":
        return DetectedDocumentFormat("odt")
    if suffix == ".rtf" or "rtf" in mime:
        return DetectedDocumentFormat("rtf")
    if suffix == ".doc" or mime in {"application/msword", "application/x-msword"}:
        return DetectedDocumentFormat("doc")
    if suffix in {".md", ".markdown"} or mime == "text/markdown":
        return DetectedDocumentFormat("markdown")
    if suffix in _TEXT_SUFFIXES or mime.startswith("text/") or mime in {
        "application/xml", "application/json", "application/x-yaml"
    }:
        return DetectedDocumentFormat("text")
    return DetectedDocumentFormat(suffix.lstrip(".") or mime or "unknown")


def _root_ole_streams(path: Path) -> list[str]:
    with olefile.OleFileIO(str(path)) as container:
        return [parts[0] for parts in container.listdir() if len(parts) == 1]


def validate_source_archive(source: Path, detected: str) -> None:
    """Reject broken source ZIP containers before any converter runs."""
    if detected not in {"docx", "odt", "epub", "pptx"} and not (
        detected == "spreadsheet" and zipfile.is_zipfile(source)
    ):
        return
    if not zipfile.is_zipfile(source):
        raise CorruptDocumentError(
            "document_container",
            f"Detected {detected} document is not a valid ZIP container",
        )
    try:
        with zipfile.ZipFile(source) as archive:
            if bad_member := archive.testzip():
                raise CorruptDocumentError(
                    "document_container", f"Corrupt ZIP member: {bad_member}"
                )
    except CorruptDocumentError:
        raise
    except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
        raise CorruptDocumentError("document_container", str(exc)) from exc


def _decode_text(payload: bytes) -> str:
    if not payload:
        raise ValueError("Source document is empty")
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        value = payload.decode("utf-16")
        if _printable_ratio(value) >= 0.85:
            return value
    try:
        value = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    else:
        if "\x00" not in value and _printable_ratio(value) >= 0.85:
            return value
    even_nuls = payload[0::2].count(0) / max(1, len(payload[0::2]))
    odd_nuls = payload[1::2].count(0) / max(1, len(payload[1::2]))
    if max(even_nuls, odd_nuls) >= 0.3:
        encoding = "utf-16-be" if even_nuls > odd_nuls else "utf-16-le"
        try:
            value = payload.decode(encoding)
        except UnicodeDecodeError:
            pass
        else:
            if "\x00" not in value and _printable_ratio(value) >= 0.85:
                return value
    candidates: list[tuple[float, str]] = []
    for encoding in ("cp1251", "cp866"):
        value = payload.decode(encoding)
        if _printable_ratio(value) >= 0.75:
            candidates.append((_legacy_text_quality(value), value))
    if candidates:
        _score, value = max(candidates, key=lambda item: item[0])
        if _printable_ratio(value) >= 0.85:
            return value
    value = payload.decode("latin-1")
    if _printable_ratio(value) < 0.75:
        raise ValueError("Could not determine a usable text encoding")
    return value


def _legacy_text_quality(value: str) -> float:
    score = 0.0
    for char in value:
        category = unicodedata.category(char)
        if char.isalnum():
            score += 2.0
        elif char.isspace() or char in ".,;:!?()[]{}<>/\\'\"-_+=*#@%&|":
            score += 1.0
        elif "\u2500" <= char <= "\u259f" or category.startswith("S"):
            score -= 2.0
        elif category.startswith("C"):
            score -= 3.0
        else:
            score -= 0.5
    return score / max(1, len(value))


def _printable_ratio(value: str) -> float:
    if not value:
        return 0.0
    printable = sum(char.isprintable() or char in "\n\r\t" for char in value)
    return printable / len(value)
