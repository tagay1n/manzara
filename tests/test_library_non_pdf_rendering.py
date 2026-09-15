"""Library non pdf rendering coverage."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.modules.library.non_pdf_extraction import (
    ExtractedAsset,
    PreparedExtraction,
    prepare_extraction,
    render_markdown,
    validate_rendered_markdown,
)


def test_docx_preserves_math_table_figure_and_public_image_url(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "source.png"
    Image.new("RGB", (16, 12), (150, 20, 20)).save(image_path)
    source = """# Demo

Inline $x^2 + y^2 = z^2$.

| A | B |
|---|---|
| 1 | two |

![A useful caption](source.png)
"""
    docx = tmp_path / "book.docx"
    subprocess.run(
        [
            "pandoc",
            "-f",
            "markdown",
            "-t",
            "docx",
            "--resource-path",
            str(tmp_path),
            "-o",
            str(docx),
        ],
        input=source,
        text=True,
        check=True,
    )
    wrongly_named = tmp_path / "cached.doc"
    wrongly_named.write_bytes(docx.read_bytes())

    prepared = prepare_extraction(
        wrongly_named,
        workspace=tmp_path / "workspace",
        mime_type="application/msword",
        source_path="incorrect.doc",
    )
    assert prepared.detected_format == "docx"
    assert len(prepared.assets) == 1
    markdown = render_markdown(
        prepared,
        asset_urls={prepared.assets[0].source_ref: "https://s3.example/images/a/1.png"},
    )

    assert "$x^{2} + y^{2} = z^{2}$" in markdown
    assert "<table>" in markdown
    assert '<figure style="text-align: center; margin: 1em 0;">' in markdown
    assert 'src="https://s3.example/images/a/1.png"' in markdown
    assert "<figcaption>A useful caption</figcaption>" in markdown
    assert "```{=html}" not in markdown
    assert (tmp_path / "workspace" / "raw-ast.json").is_file()
    assert (tmp_path / "workspace" / "final.md").is_file()
    report = validate_rendered_markdown(
        prepared,
        markdown,
        asset_urls={prepared.assets[0].source_ref: "https://s3.example/images/a/1.png"},
    )
    assert report["passed"] is True
    assert report["referenced_asset_count"] == 1


def test_consecutive_and_grouped_images_all_render_as_html_figures(
    tmp_path: Path,
) -> None:
    def image(url: str) -> dict:
        return {
            "t": "Image",
            "c": [["", [], []], [], [url, ""]],
        }

    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {"t": "Para", "c": [image("local-1.png")]},
            {"t": "Para", "c": [image("local-2.png")]},
            {
                "t": "Para",
                "c": [image("local-3.png"), {"t": "Space"}, image("local-4.png")],
            },
            {
                "t": "Div",
                "c": [
                    ["container", [], []],
                    [{"t": "Para", "c": [image("local-5.png")]}],
                ],
            },
            {
                "t": "Para",
                "c": [
                    {
                        "t": "Span",
                        "c": [
                            ["page-anchor", ["style"], []],
                            [{"t": "Str", "c": "Body text"}],
                        ],
                    }
                ],
            },
        ],
    }
    prepared = PreparedExtraction("docx", tmp_path, ast, None, ())
    urls = {
        f"local-{index}.png": f"https://public.example/{index}.png"
        for index in range(1, 6)
    }

    markdown = render_markdown(prepared, asset_urls=urls)

    assert markdown.count("<figure ") == 4
    assert markdown.count("<img ") == 5
    assert "![" not in markdown
    assert ":::" not in markdown
    assert "{.style}" not in markdown
    for url in urls.values():
        assert url in markdown
    assert "Body text" in markdown
    assert (
        validate_rendered_markdown(prepared, markdown, asset_urls=urls)["passed"]
        is True
    )


def test_images_mixed_with_text_are_split_into_html_figures(tmp_path: Path) -> None:
    def image(url: str) -> dict:
        return {"t": "Image", "c": [["", [], []], [], [url, ""]]}

    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {
                "t": "Para",
                "c": [
                    {
                        "t": "Underline",
                        "c": [{"t": "Str", "c": "Before"}],
                    },
                    {"t": "Space"},
                    image("local-1.png"),
                    {"t": "Space"},
                    image("local-2.png"),
                    {"t": "Space"},
                    {"t": "Str", "c": "After"},
                ],
            }
        ],
    }
    prepared = PreparedExtraction("rtf", tmp_path, ast, None, ())
    urls = {
        "local-1.png": "https://public.example/1.png",
        "local-2.png": "https://public.example/2.png",
    }

    markdown = render_markdown(prepared, asset_urls=urls)

    assert "Before" in markdown
    assert "After" in markdown
    assert markdown.count("<figure ") == 2
    assert markdown.count("<img ") == 2
    assert "![" not in markdown
    assert "{.underline}" not in markdown


def test_epub_local_document_links_become_plain_text(tmp_path: Path) -> None:
    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {
                "t": "Para",
                "c": [
                    {
                        "t": "Link",
                        "c": [
                            ["", [], []],
                            [{"t": "Str", "c": "Chapter one"}],
                            ["chapter.xhtml#start", ""],
                        ],
                    },
                    {"t": "Space"},
                    {
                        "t": "Link",
                        "c": [
                            ["", [], []],
                            [{"t": "Str", "c": "Publisher"}],
                            ["https://example.com/book", ""],
                        ],
                    },
                ],
            }
        ],
    }
    prepared = PreparedExtraction("epub", tmp_path, ast, None, ())

    markdown = render_markdown(prepared, asset_urls={})

    assert "Chapter one" in markdown
    assert "chapter.xhtml" not in markdown
    assert "[Publisher](https://example.com/book)" in markdown


def test_epub_heading_source_attributes_are_removed(tmp_path: Path) -> None:
    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {
                "t": "Header",
                "c": [
                    1,
                    ["chapter.xhtml#p1", ["coverTtl"], [["pid", "1"]]],
                    [{"t": "Str", "c": "Chapter"}],
                ],
            }
        ],
    }
    prepared = PreparedExtraction("epub", tmp_path, ast, None, ())

    markdown = render_markdown(prepared, asset_urls={})

    assert markdown == "# Chapter\n"


def test_validation_rejects_unmanaged_external_html_image(tmp_path: Path) -> None:
    prepared = PreparedExtraction("markdown", tmp_path, None, "Text\n", ())

    with pytest.raises(ValueError, match="unmanaged HTML image"):
        validate_rendered_markdown(
            prepared,
            '<figure><img src="https://external.example/image.jpg"></figure>\n',
            asset_urls={},
        )


def test_image_only_document_is_rejected_as_requiring_ocr(tmp_path: Path) -> None:
    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {
                "t": "Para",
                "c": [
                    {
                        "t": "Image",
                        "c": [
                            ["", [], []],
                            [],
                            ["page.jpeg", ""],
                        ],
                    }
                ],
            }
        ],
    }
    prepared = PreparedExtraction("doc", tmp_path, ast, None, ())

    with pytest.raises(ValueError, match="OCR required"):
        render_markdown(
            prepared,
            asset_urls={"page.jpeg": "https://public.example/page.jpeg"},
        )


def test_publication_validation_rejects_an_unreferenced_asset(tmp_path: Path) -> None:
    asset = tmp_path / "image.png"
    Image.new("RGB", (2, 2), "red").save(asset)
    prepared = PreparedExtraction(
        "docx",
        tmp_path,
        None,
        "Text\n",
        (ExtractedAsset("local.png", asset, 1),),
    )

    with pytest.raises(ValueError, match="not rendered as an HTML image"):
        validate_rendered_markdown(
            prepared,
            "Text\n",
            asset_urls={"local.png": "https://public.example/1.png"},
        )

    report = json.loads((tmp_path / "validation.json").read_text())
    assert report["passed"] is False
