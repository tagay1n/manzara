"""Rich local extraction for non-PDF documents."""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from html import escape
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
from typing import Any, Mapping
import unicodedata
import zipfile
from xml.etree import ElementTree

from PIL import Image


EXTRACTOR_VERSION = "nonpdf.v6"
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
    pandoc_format = detected
    if detected in {"doc", "rtf"}:
        pandoc_source = _convert_to_docx(
            source, workspace=workspace, detected_format=detected
        )
        pandoc_format = "docx"
    elif detected == "fb2":
        pandoc_source = _fb2_to_html(source, workspace=workspace)
        pandoc_format = "html"
    ast_path = workspace / "raw-ast.json"
    media_dir = workspace / "media"
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
    del result
    ast = json.loads(ast_path.read_text(encoding="utf-8"))
    # Only body blocks are rendered into the published Markdown. Avoid uploading
    # images that occur solely in Pandoc metadata and can never be referenced.
    assets = _collect_assets(
        {"blocks": ast.get("blocks", [])}, workspace=workspace
    )
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
        ast = _strip_presentational_spans(ast)
        _strip_heading_attributes(ast)
        ast = _strip_local_links(ast)
        blocks = ast.get("blocks") if isinstance(ast.get("blocks"), list) else []
        if not _has_non_image_inline_content(blocks):
            raise ValueError(
                "Extracted document contains only images; OCR required for text content"
            )
        _rewrite_image_urls(ast, asset_urls)
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


def validate_rendered_markdown(
    prepared: PreparedExtraction,
    markdown: str,
    *,
    asset_urls: Mapping[str, str],
) -> dict[str, Any]:
    """Reject incomplete image publication and retain a compact QA report."""
    errors: list[str] = []
    expected_urls: list[str] = []
    for asset in prepared.assets:
        url = str(asset_urls.get(asset.source_ref) or "")
        if not url:
            errors.append(f"missing public URL for asset {asset.ordinal}")
            continue
        expected_urls.append(url)
        quoted = re.escape(url)
        if not re.search(rf'<img\b[^>]*\bsrc=["\']{quoted}["\']', markdown):
            errors.append(f"asset {asset.ordinal} is not rendered as an HTML image")
    markdown_images = re.findall(
        r"(?m)(?<!\\)!\[[^\]\n]*\]\([^)\n]+\)", markdown
    )
    if markdown_images:
        errors.append(f"found {len(markdown_images)} Markdown image expressions")
    local_refs = [
        asset.source_ref
        for asset in prepared.assets
        if asset.source_ref and asset.source_ref in markdown
    ]
    if local_refs:
        errors.append(f"found {len(local_refs)} local media references")
    report = {
        "extractor_version": EXTRACTOR_VERSION,
        "detected_format": prepared.detected_format,
        "asset_count": len(prepared.assets),
        "referenced_asset_count": sum(url in markdown for url in expected_urls),
        "html_image_count": len(re.findall(r"<img\b", markdown)),
        "html_figure_count": len(re.findall(r"<figure\b", markdown)),
        "errors": errors,
        "passed": not errors,
    }
    (prepared.workspace / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if errors:
        raise ValueError("Rendered Markdown validation failed: " + "; ".join(errors))
    return report


def _run(
    command: list[str],
    *,
    workspace: Path,
    label: str,
    stdin: str | None = None,
    timeout_seconds: int = 300,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
    except BaseException:
        _terminate_process_group(process)
        process.communicate()
        raise
    (workspace / f"{label}.stdout.log").write_text(stdout, encoding="utf-8")
    (workspace / f"{label}.stderr.log").write_text(stderr, encoding="utf-8")
    if timed_out:
        raise RuntimeError(f"{label} timed out after {timeout_seconds} seconds")
    if process.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit code {process.returncode}: "
            f"{stderr.strip()[-1000:]}"
        )
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _convert_to_docx(
    source: Path, *, workspace: Path, detected_format: str
) -> Path:
    converted = workspace / "converted"
    converted.mkdir(parents=True, exist_ok=True)
    # LibreOffice primarily selects its input filter from the filename. Give it
    # the byte-detected suffix instead of the unreliable catalog/cache suffix.
    staged_source = workspace / f"source.{detected_format}"
    shutil.copyfile(source, staged_source)
    profile = workspace / "libreoffice-profile"
    _run(
        [
            "soffice",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--headless", "--convert-to", "docx", "--outdir", str(converted),
            str(staged_source),
        ],
        workspace=workspace,
        label="libreoffice",
        timeout_seconds=900,
    )
    matches = sorted(converted.glob("*.docx"))
    if len(matches) != 1:
        raise RuntimeError(f"LibreOffice produced {len(matches)} DOCX files")
    return matches[0]


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


def _fb2_to_html(source: Path, *, workspace: Path) -> Path:
    root = ElementTree.parse(source).getroot()
    parents = {child: parent for parent in root.iter() for child in parent}
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
            parent = parents.get(node)
            if (
                name == "p"
                and parent is not None
                and parent.tag.rsplit("}", 1)[-1] == "title"
            ):
                continue
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


def _strip_presentational_spans(value: Any) -> Any:
    if isinstance(value, list):
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, dict) and item.get("t") in {"Span", "Underline"}:
                content = item.get("c") if isinstance(item.get("c"), list) else []
                inlines = (
                    content[1]
                    if item.get("t") == "Span" and len(content) > 1
                    else content
                )
                stripped = _strip_presentational_spans(inlines)
                if isinstance(stripped, list):
                    normalized.extend(stripped)
                continue
            normalized.append(_strip_presentational_spans(item))
        return normalized
    if isinstance(value, dict):
        return {
            key: _strip_presentational_spans(item)
            for key, item in value.items()
        }
    return value


def _strip_heading_attributes(value: Any) -> None:
    for node in _walk(value):
        if node.get("t") != "Header":
            continue
        content = node.get("c")
        if isinstance(content, list) and len(content) > 1:
            content[1] = ["", [], []]


def _strip_local_links(value: Any) -> Any:
    if isinstance(value, list):
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, dict) and item.get("t") == "Link":
                content = item.get("c") if isinstance(item.get("c"), list) else []
                target = content[-1] if content else ["", ""]
                href = str(target[0] if isinstance(target, list) and target else "")
                if not href.lower().startswith(("http://", "https://", "mailto:")):
                    label = content[1] if len(content) > 1 else []
                    stripped = _strip_local_links(label)
                    if isinstance(stripped, list):
                        normalized.extend(stripped)
                    continue
            normalized.append(_strip_local_links(item))
        return normalized
    if isinstance(value, dict):
        return {
            key: _strip_local_links(item)
            for key, item in value.items()
        }
    return value


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
    dropped: list[dict[str, str]] = []
    for ref in refs:
        raw_path = Path(ref.removeprefix("file://"))
        if not raw_path.is_absolute():
            raw_path = workspace / raw_path
        if not raw_path.is_file():
            dropped.append({"source_ref": ref, "reason": "embedded media is missing"})
            continue
        ordinal = len(assets) + 1
        try:
            browser_path = _browser_image(
                raw_path, workspace=workspace, ordinal=ordinal
            )
        except Exception as exc:  # noqa: BLE001
            dropped.append(
                {
                    "source_ref": ref,
                    "reason": f"{type(exc).__name__}: {exc}"[:1000],
                }
            )
            continue
        assets.append(ExtractedAsset(ref, browser_path, ordinal))
    if dropped:
        (workspace / "dropped-media.json").write_text(
            json.dumps(dropped, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
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


def _has_non_image_inline_content(value: Any) -> bool:
    if isinstance(value, list):
        return any(_has_non_image_inline_content(item) for item in value)
    if not isinstance(value, dict):
        return False
    node_type = value.get("t")
    if node_type == "Image":
        return False
    if node_type == "Str":
        return bool(str(value.get("c") or "").strip())
    if node_type in {"Code", "Math", "RawInline", "Note"}:
        return True
    return _has_non_image_inline_content(value.get("c"))


def _is_image_only_paragraph(block: Mapping[str, Any]) -> bool:
    if block.get("t") not in {"Para", "Plain"}:
        return False
    content = block.get("c") if isinstance(block.get("c"), list) else []
    return bool(_images(content)) and not _has_non_image_inline_content(content)


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
    block = _normalize_block_children(block, ast=ast, workspace=workspace)
    if block.get("t") == "Figure":
        return {"t": "RawBlock", "c": ["html", _figure_html(block)]}
    if _is_image_only_paragraph(block):
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


def _normalize_block_children(
    block: dict[str, Any], *, ast: Mapping[str, Any], workspace: Path
) -> dict[str, Any]:
    node_type = block.get("t")
    content = block.get("c")
    if node_type == "Div" and isinstance(content, list) and len(content) > 1:
        if isinstance(content[1], list):
            content[1] = _normalize_blocks(content[1], ast=ast, workspace=workspace)
    elif node_type == "BlockQuote" and isinstance(content, list):
        block["c"] = _normalize_blocks(content, ast=ast, workspace=workspace)
    elif node_type == "BulletList" and isinstance(content, list):
        block["c"] = [
            _normalize_blocks(item, ast=ast, workspace=workspace)
            if isinstance(item, list)
            else item
            for item in content
        ]
    elif node_type == "OrderedList" and isinstance(content, list) and len(content) > 1:
        if isinstance(content[1], list):
            content[1] = [
                _normalize_blocks(item, ast=ast, workspace=workspace)
                if isinstance(item, list)
                else item
                for item in content[1]
            ]
    elif node_type == "DefinitionList" and isinstance(content, list):
        for definition in content:
            if not isinstance(definition, list) or len(definition) < 2:
                continue
            groups = definition[1]
            if isinstance(groups, list):
                definition[1] = [
                    _normalize_blocks(group, ast=ast, workspace=workspace)
                    if isinstance(group, list)
                    else group
                    for group in groups
                ]
    return block


def _normalize_blocks(
    blocks: list[dict[str, Any]], *, ast: Mapping[str, Any], workspace: Path
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.get("t") == "Div":
            content = block.get("c") if isinstance(block.get("c"), list) else []
            children = content[1] if len(content) > 1 else []
            if isinstance(children, list):
                normalized.extend(
                    _normalize_blocks(children, ast=ast, workspace=workspace)
                )
            index += 1
            continue
        if (
            block.get("t") in {"Para", "Plain"}
            and _images(block)
            and _has_non_image_inline_content(block.get("c"))
        ):
            normalized.extend(_split_mixed_image_block(block))
            index += 1
            continue
        if _is_image_only_paragraph(block):
            block_images = _images(block)
            if len(block_images) == 1:
                image_alt = _inline_text(block_images[0])
                caption = image_alt
                if index + 1 < len(blocks):
                    following = blocks[index + 1]
                    following_text = _inline_text(following)
                    if (
                        following.get("t") in {"Para", "Plain"}
                        and not _images(following)
                        and image_alt
                        and following_text == image_alt
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


def _split_mixed_image_block(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = block.get("c") if isinstance(block.get("c"), list) else []
    split: list[dict[str, Any]] = []
    text_inlines: list[dict[str, Any]] = []

    def flush_text() -> None:
        if _has_non_image_inline_content(text_inlines):
            split.append({"t": str(block.get("t") or "Para"), "c": list(text_inlines)})
        text_inlines.clear()

    for inline in content:
        if isinstance(inline, dict) and inline.get("t") == "Image":
            flush_text()
            split.append(
                {
                    "t": "RawBlock",
                    "c": ["html", _figure_html({"t": "Para", "c": [inline]})],
                }
            )
        elif isinstance(inline, dict):
            text_inlines.append(inline)
    flush_text()
    return split


__all__ = [
    "EXTRACTOR_VERSION", "ExtractedAsset", "PreparedExtraction",
    "UnsupportedDocumentFormat", "detect_document_format", "prepare_extraction",
    "render_markdown", "require_converter_binaries", "validate_rendered_markdown",
]
