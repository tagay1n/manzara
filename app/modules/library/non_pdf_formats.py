"""Byte-based document format detection and legacy text decoding."""

from __future__ import annotations

import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".xml", ".tex",
    ".srt", ".json", ".yaml", ".yml", ".ini",
}

_HTML_SUFFIXES = {".html", ".htm"}

_SUPPORTED_FORMATS = {
    "doc", "docx", "rtf", "odt", "epub", "fb2", "html", "markdown", "text", "pptx"
}


def detect_document_format(path: Path, *, mime_type: str = "", source_path: str = "") -> str:
    """Classify source bytes before considering unreliable catalog hints."""
    source = Path(path)
    with source.open("rb") as stream:
        header = stream.read(8192)
    lowered = header.lstrip().lower()
    suffix = PurePosixPath(str(source_path or source.name)).suffix.lower()
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if header.startswith(b"%PDF-"):
        return "pdf"
    if lowered.startswith(b"{\\rtf"):
        return "rtf"
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            if "word/document.xml" in names:
                return "docx"
            if "mimetype" in names:
                value = archive.read("mimetype").decode("ascii", errors="ignore").strip()
                if value == "application/epub+zip":
                    return "epub"
                if value == "application/vnd.oasis.opendocument.text":
                    return "odt"
            if "META-INF/container.xml" in names:
                return "epub"
            if any(name.startswith("ppt/") for name in names):
                if "ppt/presentation.xml" in names:
                    return "pptx"
                return "powerpoint"
            if any(name.startswith("xl/") for name in names):
                return "spreadsheet"
    if header.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        ole_markers = _find_ole_document_markers(source)
        if "powerpoint" in ole_markers:
            return "powerpoint"
        if "spreadsheet" in ole_markers:
            return "spreadsheet"
        if "doc" in ole_markers:
            return "doc"
        if suffix in {".ppt", ".pps"} or "powerpoint" in mime:
            return "powerpoint"
        if suffix == ".xls" or "excel" in mime:
            return "spreadsheet"
        if suffix == ".doc" or mime in {"application/msword", "application/x-msword"}:
            return "doc"
        return "compound"
    sample = lowered[:4096]
    if b"<fictionbook" in sample or suffix == ".fb2" or "fictionbook" in mime:
        return "fb2"
    if (
        b"<!doctype html" in sample
        or b"<html" in sample
        or suffix in _HTML_SUFFIXES
        or mime == "text/html"
    ):
        return "html"
    if suffix == ".epub" or mime == "application/epub+zip":
        return "epub"
    if suffix == ".pptx" or mime == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        return "pptx"
    if suffix == ".docx" or "wordprocessingml" in mime:
        return "docx"
    if suffix == ".odt" or mime == "application/vnd.oasis.opendocument.text":
        return "odt"
    if suffix == ".rtf" or "rtf" in mime:
        return "rtf"
    if suffix == ".doc" or mime in {"application/msword", "application/x-msword"}:
        return "doc"
    if suffix in {".md", ".markdown"} or mime == "text/markdown":
        return "markdown"
    if suffix in _TEXT_SUFFIXES or mime.startswith("text/") or mime in {
        "application/xml", "application/json", "application/x-yaml"
    }:
        return "text"
    return suffix.lstrip(".") or mime or "unknown"


def _find_ole_document_markers(path: Path) -> set[str]:
    markers = {
        "doc": "WordDocument".encode("utf-16-le"),
        "powerpoint": "PowerPoint Document".encode("utf-16-le"),
        "spreadsheet": "Workbook".encode("utf-16-le"),
        "spreadsheet-book": "Book".encode("utf-16-le"),
    }
    found: set[str] = set()
    overlap = max(len(marker) for marker in markers.values()) - 1
    previous = b""
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sample = previous + chunk
            for kind, marker in markers.items():
                if marker in sample:
                    found.add("spreadsheet" if kind == "spreadsheet-book" else kind)
            if {"doc", "powerpoint", "spreadsheet"}.issubset(found):
                break
            previous = sample[-overlap:]
    return found


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
