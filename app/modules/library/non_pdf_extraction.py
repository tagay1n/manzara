"""Rich local extraction for non-PDF documents."""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from html import escape
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from typing import Any, Mapping
import zipfile
from xml.etree import ElementTree

from PIL import Image


EXTRACTOR_VERSION = "nonpdf.v1"
_BROWSER_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".xml", ".tex",
    ".srt", ".json", ".yaml", ".yml", ".ini",
}
_HTML_SUFFIXES = {".html", ".htm"}
_SUPPORTED_FORMATS = {
    "doc", "docx", "rtf", "odt", "epub", "fb2", "html", "markdown", "text"
}


@dataclass(frozen=True)
class ExtractedAsset:
    source_ref: str
    path: Path
    ordinal: int


@dataclass(frozen=True)
class PreparedExtraction:
    detected_format: str
    workspace: Path
    ast: dict[str, Any] | None
    text: str | None
    assets: tuple[ExtractedAsset, ...]


class UnsupportedDocumentFormat(ValueError):
    def __init__(self, detected_format: str) -> None:
        self.detected_format = str(detected_format or "unknown")
        super().__init__(f"Unsupported document format: {self.detected_format}")


def require_converter_binaries() -> None:
    missing = [name for name in ("pandoc", "soffice") if shutil.which(name) is None]
    if missing:
        raise RuntimeError(
            "Missing required document conversion binaries: " + ", ".join(missing)
        )


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
                return "powerpoint"
            if any(name.startswith("xl/") for name in names):
                return "spreadsheet"
    if header.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        if suffix in {".ppt", ".pps"} or "powerpoint" in mime:
            return "powerpoint"
        if suffix in {".xls"} or "excel" in mime:
            return "spreadsheet"
        return "doc"
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


def prepare_extraction(
    source: Path,
    *,
    workspace: Path,
    mime_type: str,
    source_path: str,
) -> PreparedExtraction:
    workspace.mkdir(parents=True, exist_ok=True)
    detected = detect_document_format(
        source, mime_type=mime_type, source_path=source_path
    )
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
        text_value = _decode_text(source.read_bytes())
        if detected == "text" and not text_value.endswith("\n"):
            text_value += "\n"
        return PreparedExtraction(detected, workspace, None, text_value, ())

    pandoc_source = Path(source)
    if detected in {"doc", "rtf"}:
        pandoc_source = _convert_to_docx(source, workspace=workspace)
    elif detected == "fb2":
        pandoc_source = _fb2_to_html(source, workspace=workspace)
    ast_path = workspace / "raw-ast.json"
    media_dir = workspace / "media"
    result = _run(
        [
            "pandoc",
            str(pandoc_source),
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
    del result
    ast = json.loads(ast_path.read_text(encoding="utf-8"))
    assets = _collect_assets(ast, workspace=workspace)
    return PreparedExtraction(detected, workspace, ast, None, assets)


def render_markdown(
    prepared: PreparedExtraction,
    *,
    asset_urls: Mapping[str, str],
) -> str:
    if prepared.ast is None:
        content = str(prepared.text or "")
    else:
        ast = deepcopy(prepared.ast)
        _rewrite_image_urls(ast, asset_urls)
        blocks = ast.get("blocks") if isinstance(ast.get("blocks"), list) else []
        ast["blocks"] = _normalize_blocks(
            blocks, ast=ast, workspace=prepared.workspace
        )
        output = prepared.workspace / "final.md"
        _run(
            [
                "pandoc", "-f", "json", "-t", "markdown",
                "--wrap=preserve", "-o", str(output),
            ],
            workspace=prepared.workspace,
            label="pandoc-write",
            stdin=json.dumps(ast, ensure_ascii=False),
        )
        content = output.read_text(encoding="utf-8")
        content = re.sub(
            r"```\{=html\}\n(?P<html>.*?)\n```",
            lambda match: match.group("html"),
            content,
            flags=re.DOTALL,
        )
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    if not re.search(r"[\w\d]", content, flags=re.UNICODE):
        raise ValueError("Extracted Markdown has no textual content")
    final_path = prepared.workspace / "final.md"
    final_path.write_text(content, encoding="utf-8", newline="\n")
    return content


def _run(
    command: list[str],
    *,
    workspace: Path,
    label: str,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    (workspace / f"{label}.stdout.log").write_text(result.stdout, encoding="utf-8")
    (workspace / f"{label}.stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code {result.returncode}: "
            f"{result.stderr.strip()[-1000:]}"
        )
    return result


def _convert_to_docx(source: Path, *, workspace: Path) -> Path:
    converted = workspace / "converted"
    converted.mkdir(parents=True, exist_ok=True)
    profile = workspace / "libreoffice-profile"
    _run(
        [
            "soffice",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--headless", "--convert-to", "docx", "--outdir", str(converted),
            str(source),
        ],
        workspace=workspace,
        label="libreoffice",
    )
    matches = sorted(converted.glob("*.docx"))
    if len(matches) != 1:
        raise RuntimeError(f"LibreOffice produced {len(matches)} DOCX files")
    return matches[0]


def _decode_text(payload: bytes) -> str:
    if not payload:
        raise ValueError("Source document is empty")
    encodings = ["utf-8-sig", "utf-16", "cp1251", "cp866"]
    for encoding in encodings:
        try:
            value = payload.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" not in value and _printable_ratio(value) >= 0.85:
            return value
    value = payload.decode("latin-1")
    if _printable_ratio(value) < 0.75:
        raise ValueError("Could not determine a usable text encoding")
    return value


def _printable_ratio(value: str) -> float:
    if not value:
        return 0.0
    printable = sum(char.isprintable() or char in "\n\r\t" for char in value)
    return printable / len(value)


def _fb2_to_html(source: Path, *, workspace: Path) -> Path:
    root = ElementTree.parse(source).getroot()
    images_dir = workspace / "fb2-media"
    images_dir.mkdir(parents=True, exist_ok=True)
    image_paths: dict[str, Path] = {}
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "binary":
            continue
        identifier = str(node.attrib.get("id") or "").strip()
        if not identifier or not str(node.text or "").strip():
            continue
        content_type = str(node.attrib.get("content-type") or "image/png").lower()
        suffix = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "image/webp": ".webp", "image/svg+xml": ".svg",
        }.get(content_type, ".bin")
        destination = images_dir / f"{identifier}{suffix}"
        destination.write_bytes(base64.b64decode("".join(str(node.text).split())))
        image_paths[identifier] = destination
    parts = ["<!doctype html><html><body>"]
    for node in root.iter():
        name = node.tag.rsplit("}", 1)[-1]
        text_value = " ".join("".join(node.itertext()).split())
        if name == "title" and text_value:
            parts.append(f"<h2>{escape(text_value)}</h2>")
        elif name in {"p", "subtitle", "text-author"} and text_value:
            parts.append(f"<p>{escape(text_value)}</p>")
        elif name == "image":
            href = next(
                (str(value).lstrip("#") for key, value in node.attrib.items() if key.endswith("href")),
                "",
            )
            if href in image_paths:
                parts.append(f'<figure><img src="{escape(str(image_paths[href]), quote=True)}"></figure>')
    parts.append("</body></html>")
    destination = workspace / "fb2.html"
    destination.write_text("\n".join(parts), encoding="utf-8")
    return destination


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _collect_assets(ast: Mapping[str, Any], *, workspace: Path) -> tuple[ExtractedAsset, ...]:
    refs: list[str] = []
    for node in _walk(ast):
        if node.get("t") != "Image":
            continue
        target = node.get("c", [None, None, ["", ""]])[-1]
        ref = str(target[0] if isinstance(target, list) and target else "")
        if ref.startswith(("http://", "https://")) or not ref:
            continue
        if ref not in refs:
            refs.append(ref)
    assets: list[ExtractedAsset] = []
    for ordinal, ref in enumerate(refs, start=1):
        raw_path = Path(ref.removeprefix("file://"))
        if not raw_path.is_absolute():
            raw_path = workspace / raw_path
        if not raw_path.is_file():
            continue
        browser_path = _browser_image(raw_path, workspace=workspace, ordinal=ordinal)
        assets.append(ExtractedAsset(ref, browser_path, ordinal))
    return tuple(assets)


def _browser_image(path: Path, *, workspace: Path, ordinal: int) -> Path:
    suffix = path.suffix.lower()
    if suffix in _BROWSER_IMAGE_SUFFIXES:
        return path
    destination = workspace / "normalized-media" / f"{ordinal}.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(path) as image:
            image.convert("RGBA" if image.mode in {"RGBA", "LA"} else "RGB").save(
                destination, "PNG"
            )
            return destination
    except Exception:
        converted = destination.parent / f"convert-{ordinal}"
        converted.mkdir(parents=True, exist_ok=True)
        _run(
            ["soffice", "--headless", "--convert-to", "png", "--outdir", str(converted), str(path)],
            workspace=workspace,
            label=f"image-convert-{ordinal}",
        )
        matches = sorted(converted.glob("*.png"))
        if len(matches) != 1:
            raise RuntimeError(f"Could not convert embedded image {path.name} to PNG")
        shutil.copyfile(matches[0], destination)
        return destination


def _rewrite_image_urls(value: Any, asset_urls: Mapping[str, str]) -> None:
    for node in _walk(value):
        if node.get("t") != "Image":
            continue
        target = node.get("c", [None, None, ["", ""]])[-1]
        if not isinstance(target, list) or not target:
            continue
        source_ref = str(target[0])
        if source_ref in asset_urls:
            target[0] = str(asset_urls[source_ref])
        elif source_ref and not source_ref.startswith(("http://", "https://")):
            # Pandoc can retain a missing or unsupported embedded-object path.
            # Drop it instead of publishing a broken local filesystem reference.
            node.clear()
            node.update({"t": "Str", "c": ""})


def _inline_text(value: Any) -> str:
    chunks: list[str] = []
    for node in _walk(value):
        if node.get("t") == "Str":
            chunks.append(str(node.get("c") or ""))
        elif node.get("t") in {"Space", "SoftBreak", "LineBreak"}:
            chunks.append(" ")
    return " ".join("".join(chunks).split())


def _images(value: Any) -> list[dict[str, Any]]:
    return [node for node in _walk(value) if node.get("t") == "Image"]


def _figure_html(value: Any, *, caption_override: str | None = None) -> str:
    images = _images(value)
    if not images:
        return ""
    if caption_override is not None:
        caption = caption_override
    elif isinstance(value, dict) and value.get("t") == "Figure":
        content = value.get("c") if isinstance(value.get("c"), list) else []
        caption = _inline_text(content[1] if len(content) > 1 else [])
    else:
        caption = ""
    parts = ['<figure style="text-align: center; margin: 1em 0;">']
    for image in images:
        content = image.get("c") if isinstance(image.get("c"), list) else []
        alt = _inline_text(content[1] if len(content) > 1 else [])
        target = content[-1] if content else ["", ""]
        url = str(target[0] if isinstance(target, list) and target else "")
        parts.append(
            f'<img alt="{escape(alt, quote=True)}" src="{escape(url, quote=True)}" '
            'style="max-width: 800px; width: 50%; height: auto;">'
        )
    if caption:
        parts.append(f"<figcaption>{escape(caption)}</figcaption>")
    parts.append("</figure>")
    return "".join(parts)


def _normalize_block(
    block: dict[str, Any], *, ast: Mapping[str, Any], workspace: Path
) -> dict[str, Any]:
    if block.get("t") == "Figure":
        return {"t": "RawBlock", "c": ["html", _figure_html(block)]}
    if block.get("t") in {"Para", "Plain"}:
        content = block.get("c") if isinstance(block.get("c"), list) else []
        if len(content) == 1 and isinstance(content[0], dict) and content[0].get("t") == "Image":
            return {"t": "RawBlock", "c": ["html", _figure_html(block)]}
    if block.get("t") == "Table":
        table_ast = {
            "pandoc-api-version": ast.get("pandoc-api-version", [1, 22, 2, 1]),
            "meta": {},
            "blocks": [block],
        }
        result = _run(
            ["pandoc", "-f", "json", "-t", "html"],
            workspace=workspace,
            label=f"table-{abs(hash(json.dumps(block, sort_keys=True))) % 10**8}",
            stdin=json.dumps(table_ast, ensure_ascii=False),
        )
        return {"t": "RawBlock", "c": ["html", result.stdout.strip()]}
    return block


def _normalize_blocks(
    blocks: list[dict[str, Any]], *, ast: Mapping[str, Any], workspace: Path
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.get("t") in {"Para", "Plain"}:
            content = block.get("c") if isinstance(block.get("c"), list) else []
            if (
                len(content) == 1
                and isinstance(content[0], dict)
                and content[0].get("t") == "Image"
            ):
                image_alt = _inline_text(content[0])
                caption = image_alt
                if index + 1 < len(blocks):
                    following = blocks[index + 1]
                    following_text = _inline_text(following)
                    if following.get("t") in {"Para", "Plain"} and (
                        not image_alt or following_text == image_alt
                    ):
                        caption = following_text
                        index += 1
                normalized.append(
                    {
                        "t": "RawBlock",
                        "c": ["html", _figure_html(block, caption_override=caption)],
                    }
                )
                index += 1
                continue
        normalized.append(_normalize_block(block, ast=ast, workspace=workspace))
        index += 1
    return normalized


__all__ = [
    "EXTRACTOR_VERSION", "ExtractedAsset", "PreparedExtraction",
    "UnsupportedDocumentFormat", "detect_document_format", "prepare_extraction",
    "render_markdown", "require_converter_binaries",
]
