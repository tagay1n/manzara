"""Pandoc AST cleanup and rich HTML figure/table normalization."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any, Mapping

from app.modules.library.non_pdf_converters import _run


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
