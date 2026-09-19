"""Library non pdf media coverage."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from app.modules.library.non_pdf_extraction import (
    PreparedExtraction,
    prepare_extraction,
    render_markdown,
    validate_rendered_markdown,
)
from app.modules.library.non_pdf_media import _collect_assets


def test_fb2_embedded_image_enters_common_asset_pipeline(tmp_path: Path) -> None:
    raw_image = tmp_path / "image.png"
    Image.new("RGB", (3, 3), "blue").save(raw_image)
    import base64

    payload = base64.b64encode(raw_image.read_bytes()).decode("ascii")
    fb2 = tmp_path / "book.fb2"
    fb2.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns:l="http://www.w3.org/1999/xlink">
  <body><section><title><p>Title</p></title><p>Text</p>
  <image l:href="#cover"/></section></body>
  <binary id="cover" content-type="image/png">%s</binary>
</FictionBook>"""
        % payload,
        encoding="utf-8",
    )

    prepared = prepare_extraction(
        fb2,
        workspace=tmp_path / "workspace-fb2",
        mime_type="text/xml",
        source_path="book.fb2",
    )

    assert prepared.detected_format == "fb2"
    assert len(prepared.assets) == 1
    assert (
        json.loads((tmp_path / "workspace-fb2" / "detection.json").read_text())[
            "detected_format"
        ]
        == "fb2"
    )
    markdown = render_markdown(
        prepared,
        asset_urls={prepared.assets[0].source_ref: "https://public.example/cover.png"},
    )
    assert markdown.count("Title") == 1


def test_unsupported_embedded_media_is_dropped_without_failing_document(
    tmp_path: Path,
    monkeypatch,
) -> None:
    media = tmp_path / "media.bin"
    media.write_bytes(b"unsupported proprietary object")
    ast = {
        "pandoc-api-version": [1, 23, 1],
        "meta": {},
        "blocks": [
            {"t": "Para", "c": [{"t": "Str", "c": "Readable text"}]},
            {
                "t": "Para",
                "c": [
                    {
                        "t": "Image",
                        "c": [
                            ["", [], []],
                            [{"t": "Str", "c": "Object"}],
                            [str(media), ""],
                        ],
                    }
                ],
            },
        ],
    }

    def fail_conversion(*_args, **_kwargs):
        raise RuntimeError("unsupported image encoding")

    monkeypatch.setattr(
        "app.modules.library.non_pdf_media._browser_image", fail_conversion
    )
    assets = _collect_assets(ast, workspace=tmp_path)
    prepared = PreparedExtraction("docx", tmp_path, ast, None, assets)
    markdown = render_markdown(prepared, asset_urls={})

    assert assets == ()
    assert "Readable text" in markdown
    assert str(media) not in markdown
    drops = json.loads((tmp_path / "dropped-media.json").read_text())
    assert drops[0]["source_ref"] == str(media)
    assert "unsupported image encoding" in drops[0]["reason"]
