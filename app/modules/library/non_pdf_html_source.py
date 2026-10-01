"""Normalize legacy HTML bytes and prose-only layout tables for Pandoc."""

from __future__ import annotations

import codecs
import re
from pathlib import Path

from app.modules.library.corrupt_document import CorruptDocumentError
from app.modules.library.non_pdf_formats import _decode_text

_CHARSET = re.compile(rb"(?:encoding|charset)\s*=\s*['\"]?([a-zA-Z0-9._-]+)", re.I)
_TABLE_TAG = re.compile(r"</?(?:table|tbody|thead|tfoot|tr|td|th)\b[^>]*>", re.I)
_PARAGRAPH = re.compile(r"<p\b", re.I)
_CELL = re.compile(r"<t[dh]\b", re.I)


def prepare_html_source(source: Path, *, workspace: Path) -> Path:
    raw = source.read_bytes()
    match = _CHARSET.search(raw[:4096])
    try:
        encoding = codecs.lookup(match.group(1).decode("ascii")).name if match else None
        try:
            html = raw.decode(encoding, errors="strict") if encoding else _decode_text(raw)
        except UnicodeDecodeError:
            html = _decode_text(raw)
    except (LookupError, ValueError) as exc:
        raise CorruptDocumentError("text_decode", str(exc)) from exc

    html = re.sub(r"(\b(?:encoding|charset)\s*=\s*['\"]?)[\w.-]+", r"\g<1>utf-8", html, flags=re.I)
    first_table = re.search(r"<table\b", html, re.I)
    last_table_end = list(re.finditer(r"</table\s*>", html, re.I))
    table_region = html[first_table.start():last_table_end[-1].end()] if first_table and last_table_end else ""
    paragraph_count = len(_PARAGRAPH.findall(table_region))
    cell_count = len(_CELL.findall(table_region))
    if paragraph_count >= 100 and cell_count and paragraph_count >= 4 * cell_count:
        # Legacy pages often use a small table as a page-wide prose wrapper.
        html = _TABLE_TAG.sub(lambda tag: "</div>" if tag.group().startswith("</") else "<div>", html)
    output = workspace / "normalized-source.html"
    output.write_text(html, encoding="utf-8")
    return output
