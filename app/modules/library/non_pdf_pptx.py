"""Native PPTX text extraction; defer decks with slide-level visual content."""

from __future__ import annotations

import json
import posixpath
import re
import zipfile
from html import escape, unescape
from pathlib import Path
from xml.etree import ElementTree as ET

from app.modules.library.corrupt_document import CorruptDocumentError
from app.modules.library.non_pdf_types import DeferredDocumentExtraction

MAX_XML_BYTES = 16 * 1024 * 1024
_IMAGE_TAGS = {"pic", "blip", "blipFill", "imgLayer"}
_VISUAL_TAGS = {
    "oleObj",
    "chart",
    "relIds",
    "oMath",
    "oMathPara",
    "videoFile",
    "audioFile",
    "contentPart",
    "ink",
    "AlternateContent",
}


def _xml(archive: zipfile.ZipFile, member: str) -> ET.Element:
    info = archive.getinfo(member)
    if info.file_size > MAX_XML_BYTES:
        raise DeferredDocumentExtraction("pptx", "pptx_xml_limit")
    payload = archive.read(member)
    declaration = payload.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in declaration or b"<!ENTITY" in declaration:
        raise ValueError("DTD and entities are not allowed in PPTX XML")
    root = ET.fromstring(payload)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
        node.attrib = {
            (
                "relationship_id" if key.endswith("}id") else key.rsplit("}", 1)[-1]
            ): value
            for key, value in node.attrib.items()
        }
    return root


def _slides(archive: zipfile.ZipFile):
    presentation = _xml(archive, "ppt/presentation.xml")
    relationships = _xml(archive, "ppt/_rels/presentation.xml.rels")
    targets = {}
    for rel in relationships:
        if rel.get("Type", "").endswith("/slide"):
            if rel.get("TargetMode") == "External":
                raise ValueError("Slide relationship cannot be external")
            target = rel.get("Target", "")
            resolved = posixpath.normpath(
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.join("ppt", target)
            )
            if not resolved.startswith("ppt/") or "\\" in resolved:
                raise ValueError("Invalid slide relationship target")
            targets[rel.get("Id")] = resolved
    ids = presentation.find("sldIdLst")
    if ids is None:
        raise ValueError("Missing presentation slide list")
    for index, slide_id in enumerate(ids, 1):
        target = targets.get(slide_id.get("relationship_id"))
        if target is None:
            raise ValueError("Missing slide relationship")
        yield index, target, _xml(archive, target)


def _visuals(root: ET.Element) -> tuple[bool, list[str]]:
    images = False
    visuals = set()
    for node in root.iter():
        if node.tag in _IMAGE_TAGS:
            images = True
        if node.tag in _VISUAL_TAGS:
            visuals.add(node.tag)
        if node.tag == "graphicData" and not node.get("uri", "").endswith("/table"):
            # Native tables are the sole supported graphic-frame payload.
            if node.find("tbl") is None:
                visuals.add(node.get("uri") or "unknown_graphic")
    return images, sorted(visuals)


def _text(paragraph: ET.Element) -> str:
    return "".join(
        "\n" if node.tag == "br" else node.text or ""
        for node in paragraph.iter()
        if node.tag in {"t", "br"}
    )


def _paragraphs(body: ET.Element) -> str:
    output = []
    lists = []
    for paragraph in body.findall("p"):
        value = _text(paragraph)
        if not value.strip():
            continue
        props = paragraph.find("pPr")
        bullet = (
            props is not None
            and props.find("buNone") is None
            and (
                props.find("buChar") is not None or props.find("buAutoNum") is not None
            )
        )
        tag = (
            ("ol" if props.find("buAutoNum") is not None else "ul") if bullet else None
        )
        content = escape(value).replace("\n", "<br/>")
        if not bullet:
            while lists:
                output.append(f"</li></{lists.pop()}>")
            output.append(f"<p>{content}</p>")
            continue
        level = props.get("lvl", "0")
        if not level.isdigit() or not 0 <= int(level) <= 8:
            raise ValueError("Invalid paragraph list level")
        depth = min(int(level) + 1, len(lists) + 1)
        while len(lists) > depth:
            output.append(f"</li></{lists.pop()}>")
        if len(lists) == depth and lists[-1] != tag:
            output.append(f"</li></{lists.pop()}>")
        if len(lists) == depth:
            output.append("</li><li>")
        else:
            attrs = ""
            numbering = props.find("buAutoNum")
            if numbering is not None:
                start = numbering.get("startAt", "1")
                if not start.isdigit() or int(start) < 1:
                    raise ValueError("Invalid list starting number")
                attrs = f' start="{int(start)}"'
            output.append(f"<{tag}{attrs}><li>")
            lists.append(tag)
        output.append(content)
    while lists:
        output.append(f"</li></{lists.pop()}>")
    return "".join(output)


def _table(table: ET.Element) -> str:
    rows = []
    for row in table.findall("tr"):
        cells = []
        for cell in row.findall("tc"):
            if cell.get("hMerge") in {"1", "true"} or cell.get("vMerge") in {
                "1",
                "true",
            }:
                continue
            attrs = ""
            for field, attr in [("gridSpan", "colspan"), ("rowSpan", "rowspan")]:
                raw = cell.get(field, "1")
                if not raw.isdigit() or int(raw) < 1:
                    raise ValueError("Invalid table cell span")
                if int(raw) > 1:
                    attrs += f' {attr}="{int(raw)}"'
            body = cell.find("txBody")
            cells.append(
                f"<td{attrs}>{_paragraphs(body) if body is not None else ''}</td>"
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table>" + "".join(rows) + "</table>"


def _position(shape: ET.Element, transform):
    sx, sy, ox, oy = transform
    xfrm = shape.find("spPr/xfrm")
    if xfrm is None:
        xfrm = shape.find("xfrm")
    off = xfrm.find("off") if xfrm is not None else None
    x = float(off.get("x", "0")) if off is not None else 0
    y = float(off.get("y", "0")) if off is not None else 0
    return y * sy + oy, x * sx + ox


def _shapes(tree: ET.Element, transform=(1.0, 1.0, 0.0, 0.0)):
    for shape in tree:
        if shape.tag == "grpSp":
            group = shape.find("grpSpPr/xfrm")
            child_transform = transform
            if group is not None:
                off, ext, ch_off, ch_ext = (
                    group.find(tag) for tag in ("off", "ext", "chOff", "chExt")
                )
                if all(node is not None for node in (off, ext, ch_off, ch_ext)):
                    sx, sy, ox, oy = transform
                    gx = float(ext.get("cx", "0")) / max(
                        1, float(ch_ext.get("cx", "0"))
                    )
                    gy = float(ext.get("cy", "0")) / max(
                        1, float(ch_ext.get("cy", "0"))
                    )
                    child_transform = (
                        sx * gx,
                        sy * gy,
                        ox
                        + sx
                        * (float(off.get("x", "0")) - gx * float(ch_off.get("x", "0"))),
                        oy
                        + sy
                        * (float(off.get("y", "0")) - gy * float(ch_off.get("y", "0"))),
                    )
            yield from _shapes(shape, child_transform)
        elif shape.tag in {"sp", "graphicFrame", "cxnSp"}:
            yield _position(shape, transform), shape


def _slide_html(root: ET.Element) -> str:
    tree = root.find("cSld/spTree")
    if tree is None:
        raise ValueError("Missing slide shape tree")
    output = []
    for _, shape in sorted(_shapes(tree), key=lambda item: item[0]):
        body = shape.find("txBody")
        if body is not None:
            ph = shape.find("nvSpPr/nvPr/ph")
            if ph is not None and ph.get("type") in {"title", "ctrTitle"}:
                title = "\n".join(_text(p) for p in body.findall("p"))
                if title.strip():
                    output.append("<h2>" + escape(title) + "</h2>")
            else:
                output.append(_paragraphs(body))
        for table in shape.iter("tbl"):
            output.append(_table(table))
    return "".join(output)


def pptx_to_html(source: Path, *, workspace: Path) -> Path:
    report = {
        "kind": "library.pptx_inspection",
        "slide_count": 0,
        "visible_slide_count": 0,
        "image_slide_count": 0,
        "unsupported_visual_slide_count": 0,
        "slides": [],
        "reasons": [],
        "inspection_complete": False,
    }
    fragments = []
    try:
        with zipfile.ZipFile(source) as archive:
            for index, member, root in _slides(archive):
                report["slide_count"] += 1
                if root.get("show") in {"0", "false"}:
                    continue
                report["visible_slide_count"] += 1
                images, visuals = _visuals(root)
                report["image_slide_count"] += int(images)
                report["unsupported_visual_slide_count"] += int(bool(visuals))
                report["slides"].append(
                    {
                        "slide": index,
                        "member": member,
                        "has_images": images,
                        "unsupported_visuals": visuals,
                    }
                )
                fragments.append((index, _slide_html(root)))
    except DeferredDocumentExtraction as exc:
        report["reasons"].append(exc.reason)
        _write_report(workspace, report)
        raise
    except (ET.ParseError, KeyError, ValueError, zipfile.BadZipFile, EOFError) as exc:
        raise CorruptDocumentError(
            "document_parse", f"Invalid PPTX package: {exc}"
        ) from exc
    report["inspection_complete"] = True
    if report["image_slide_count"]:
        report["reasons"].append("pptx_slide_images")
    if report["unsupported_visual_slide_count"]:
        report["reasons"].append("pptx_unsupported_visuals")
    if not any(
        re.search(r"\w", unescape(re.sub("<[^>]+>", "", content)))
        for _, content in fragments
    ):
        report["reasons"].append("pptx_no_text")
    _write_report(workspace, report)
    if report["reasons"]:
        raise DeferredDocumentExtraction("pptx", report["reasons"][0])
    destination = workspace / "pptx-native.html"
    destination.write_text(
        "<html><body>"
        + "".join(
            f"<section><h1>Slide {index}</h1>{content}</section>"
            for index, content in fragments
        )
        + " </body></html>",
        encoding="utf-8",
    )
    return destination


def _write_report(workspace: Path, report: dict) -> None:
    (workspace / "pptx-inspection.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
