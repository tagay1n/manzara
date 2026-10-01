"""Stream oversized DOCX body XML into compact HTML for Pandoc."""

from __future__ import annotations

import posixpath
import zipfile
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET

from app.modules.library.non_pdf_types import ConverterCommandError

LARGE_DOCX_XML_BYTES = 64 * 1024 * 1024
_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_RELATIONSHIP_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"


def _image_targets(archive: zipfile.ZipFile) -> dict[str, str]:
    member = "word/_rels/document.xml.rels"
    if member not in archive.namelist():
        return {}
    relationships = ET.fromstring(archive.read(member))
    targets = {}
    for relationship in relationships:
        if not relationship.get("Type", "").endswith("/image"):
            continue
        if relationship.get("TargetMode") == "External":
            continue
        target = relationship.get("Target", "")
        resolved = posixpath.normpath(posixpath.join("word", target))
        if (
            not target
            or target.startswith("/")
            or "\\" in target
            or not resolved.startswith("word/")
            or resolved not in archive.namelist()
        ):
            raise ConverterCommandError("DOCX image relationship is invalid")
        targets[relationship.get("Id", "")] = resolved
    return targets


def _paragraph_html(
    paragraph: ET.Element,
    *,
    archive: zipfile.ZipFile,
    targets: dict[str, str],
    media_dir: Path,
    copied: dict[str, str],
) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == _WORD + "t":
            parts.append(escape(node.text or ""))
        elif node.tag == _WORD + "tab":
            parts.append("&#9;")
        elif node.tag in {_WORD + "br", _WORD + "cr"}:
            parts.append("<br/>")
        elif node.tag == _DRAWING + "blip":
            relationship = node.get(_RELATIONSHIP_ID, "")
            member = targets.get(relationship)
            if member is None:
                raise ConverterCommandError("DOCX embedded image has no usable target")
            if member not in copied:
                image_name = f"image-{len(copied) + 1}{Path(member).suffix.lower()}"
                media_dir.mkdir(parents=True, exist_ok=True)
                (media_dir / image_name).write_bytes(archive.read(member))
                copied[member] = image_name
            parts.append(f'<img src="media/{copied[member]}" alt=""/>')
    return "".join(parts)


def maybe_convert_large_docx_to_html(source: Path, *, workspace: Path) -> Path | None:
    """Use a compact, streaming HTML body only when Word XML is exceptionally large.

    Tables stay on the conventional Pandoc path so their structure is retained.
    """
    with zipfile.ZipFile(source) as archive:
        try:
            xml_size = archive.getinfo("word/document.xml").file_size
        except KeyError:
            return None
        if xml_size < LARGE_DOCX_XML_BYTES:
            return None
        targets = _image_targets(archive)
        converted = workspace / "large-docx-html"
        converted.mkdir(parents=True, exist_ok=True)
        destination = converted / "source.html"
        copied: dict[str, str] = {}
        has_table = False
        with archive.open("word/document.xml") as xml, destination.open(
            "w", encoding="utf-8"
        ) as html:
            html.write('<!doctype html><html><head><meta charset="utf-8"/></head><body>\n')
            for event, element in ET.iterparse(xml, events=("start", "end")):
                if event == "start" and element.tag == _WORD + "tbl":
                    has_table = True
                    break
                if event != "end" or element.tag != _WORD + "p":
                    continue
                content = _paragraph_html(
                    element,
                    archive=archive,
                    targets=targets,
                    media_dir=converted / "media",
                    copied=copied,
                )
                if content.strip():
                    html.write(f"<p>{content}</p>\n")
                element.clear()
            html.write("</body></html>\n")
        if has_table:
            destination.unlink()
            return None
    return destination
