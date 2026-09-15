"""Markdown rendering and publication completeness validation."""

from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from app.modules.library.non_pdf_converters import _run
from app.modules.library.non_pdf_media import (
    _HTML_IMAGE_SRC_PATTERN,
    _rewrite_image_urls,
    _rewrite_source_markdown_images,
)
from app.modules.library.non_pdf_pandoc import (
    _has_non_image_inline_content,
    _normalize_blocks,
    _strip_heading_attributes,
    _strip_local_links,
    _strip_presentational_spans,
)
from app.modules.library.non_pdf_types import (
    EXTRACTOR_VERSION,
    PreparedExtraction,
)


MARKDOWN_READER = (
    "markdown-smart-markdown_in_html_blocks-native_divs-native_spans"
    "-yaml_metadata_block-pandoc_title_block"
)
MARKDOWN_WRITER = "markdown-smart-raw_attribute"


def _protect_code_fences(content: str) -> tuple[str, dict[str, str]]:
    """Keep fenced code verbatim: Pandoc drops trailing code blank lines."""
    lines = content.splitlines(keepends=True)
    protected: dict[str, str] = {}
    output: list[str] = []
    index = 0
    while index < len(lines):
        opening = re.match(
            r"^([ \t]*(?:>[ \t]*)*)(`{3,}|~{3,})([^\n]*)\n$", lines[index]
        )
        if opening is None:
            output.append(lines[index])
            index += 1
            continue
        indent, fence, info = opening.groups()
        closing = re.compile(
            r"^"
            + re.escape(indent)
            + re.escape(fence[0])
            + "{"
            + str(len(fence))
            + r",}[ \t]*(?:\n)?$"
        )
        end = next(
            (i for i in range(index + 1, len(lines)) if closing.match(lines[i])), None
        )
        if end is None:
            output.append(lines[index])
            index += 1
            continue
        marker = f"<!-- manzara-code-{uuid.uuid4().hex} -->"
        block = [
            line[len(indent) :] if line.startswith(indent) else line
            for line in lines[index : end + 1]
        ]
        code = "".join(block[1:-1])
        char = "~" if "`" in info else "`"
        size = max(
            [3, *(len(run) + 1 for run in re.findall(re.escape(char) + r"+", code))]
        )
        canonical_fence = char * size
        block[0] = canonical_fence + (" " + info.strip() if info.strip() else "") + "\n"
        block[-1] = canonical_fence
        protected[marker] = "".join(block)
        output.append(indent + marker + "\n")
        index = end + 1
    return "".join(output), protected


def format_markdown(
    content: str,
    *,
    workspace: Path,
    literal_text: bool = False,
) -> str:
    """Use one Pandoc formatting contract, retaining raw HTML and punctuation."""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    parsing_input, code_fences = (
        _protect_code_fences(content) if not literal_text else ("", {})
    )
    parsed = _run(
        ["pandoc", "-f", MARKDOWN_READER, "-t", "json"],
        workspace=workspace,
        label="markdown-format-read",
        stdin=parsing_input,
    )
    ast = json.loads(parsed.stdout)
    if literal_text:
        ast["blocks"] = []
        for paragraph in re.split(r"\n\s*\n", content.strip()):
            inlines = []
            for index, line in enumerate(paragraph.splitlines()):
                if index:
                    inlines.append({"t": "LineBreak"})
                inlines.append({"t": "Str", "c": line})
            ast["blocks"].append({"t": "Para", "c": inlines})
    ast = _strip_presentational_spans(ast)
    _strip_heading_attributes(ast)
    ast = _strip_local_links(ast)
    ast["blocks"] = _normalize_blocks(
        ast.get("blocks", []),
        ast=ast,
        workspace=workspace,
    )
    rendered = _run(
        ["pandoc", "-f", "json", "-t", MARKDOWN_WRITER, "--wrap=none"],
        workspace=workspace,
        label="markdown-format-write",
        stdin=json.dumps(ast, ensure_ascii=False),
    )
    output = rendered.stdout
    for marker, code in code_fences.items():
        if marker not in output:
            raise ValueError("Markdown formatting lost a fenced code block")
        output, restored = re.subn(
            r"(?m)^([ \t]*(?:>[ \t]*)*)" + re.escape(marker),
            lambda match: match[1] + code.replace("\n", "\n" + match[1]),
            output,
        )
        if restored != 1:
            raise ValueError(
                "Markdown formatting could not restore a fenced code block"
            )
    return output.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def render_markdown(
    prepared: PreparedExtraction,
    *,
    asset_urls: Mapping[str, str],
) -> str:
    if prepared.ast is None:
        content = str(prepared.text or "")
        if prepared.detected_format == "markdown":
            content = _rewrite_source_markdown_images(content, asset_urls)
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
        ast["blocks"] = _normalize_blocks(blocks, ast=ast, workspace=prepared.workspace)
        output = prepared.workspace / "unformatted.md"
        _run(
            [
                "pandoc",
                "-f",
                "json",
                "-t",
                MARKDOWN_WRITER,
                "--wrap=preserve",
                "-o",
                str(output),
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
    (prepared.workspace / "unformatted.md").write_text(content, encoding="utf-8")
    content = format_markdown(
        content,
        workspace=prepared.workspace,
        literal_text=prepared.detected_format == "text",
    )
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
    markdown_images = re.findall(r"(?m)(?<!\\)!\[[^\]\n]*\]\([^)\n]+\)", markdown)
    if markdown_images:
        errors.append(f"found {len(markdown_images)} Markdown image expressions")
    local_refs = [
        asset.source_ref
        for asset in prepared.assets
        if asset.source_ref and asset.source_ref in markdown
    ]
    if local_refs:
        errors.append(f"found {len(local_refs)} local media references")
    managed_urls = {str(url) for url in asset_urls.values() if str(url)}
    html_image_urls = [
        match.group("url") for match in _HTML_IMAGE_SRC_PATTERN.finditer(markdown)
    ]
    unmanaged_urls = [url for url in html_image_urls if url not in managed_urls]
    if unmanaged_urls:
        errors.append(f"found {len(unmanaged_urls)} unmanaged HTML image URLs")
    report = {
        "extractor_version": EXTRACTOR_VERSION,
        "detected_format": prepared.detected_format,
        "asset_count": len(prepared.assets),
        "referenced_asset_count": sum(url in markdown for url in expected_urls),
        "html_image_count": len(re.findall(r"<img\b", markdown)),
        "html_figure_count": len(re.findall(r"<figure\b", markdown)),
        "unmanaged_html_image_count": len(unmanaged_urls),
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
