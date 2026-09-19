"""Embedded media collection, conversion, and URL rewriting."""

from __future__ import annotations

import json
import re
import shutil
from html import escape
from pathlib import Path
from typing import Any, Mapping
from PIL import Image

from app.modules.library.non_pdf_converters import _run
from app.modules.library.non_pdf_pandoc import _walk
from app.modules.library.non_pdf_types import ExtractedAsset

_BROWSER_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"(?m)(?<!\\)!\[(?P<alt>[^\]\n]*)\]\((?P<url>https?://[^)\n]+)\)"
)

_HTML_IMAGE_SRC_PATTERN = re.compile(
    r"(?is)(?P<prefix><img\b[^>]*?\bsrc=[\"'])(?P<url>[^\"']+)(?P<suffix>[\"'])"
)


def _rewrite_source_markdown_images(
    markdown: str, asset_urls: Mapping[str, str]
) -> str:
    def replace_markdown(match: re.Match[str]) -> str:
        source = str(match.group("url"))
        url = str(asset_urls.get(source) or "")
        if not url:
            return match.group(0)
        alt = str(match.group("alt") or "")
        return (
            '<figure style="text-align: center; margin: 1em 0;">'
            f'<img alt="{escape(alt, quote=True)}" '
            f'src="{escape(url, quote=True)}" '
            'style="max-width: 800px; width: 50%; height: auto;">'
            "</figure>"
        )

    content = _MARKDOWN_IMAGE_PATTERN.sub(replace_markdown, markdown)

    def replace_html_src(match: re.Match[str]) -> str:
        source = str(match.group("url"))
        url = str(asset_urls.get(source) or source)
        return (
            f'{match.group("prefix")}{escape(url, quote=True)}'
            f'{match.group("suffix")}'
        )

    return _HTML_IMAGE_SRC_PATTERN.sub(replace_html_src, content)


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
