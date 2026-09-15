"""Native PPTX extraction and conservative visual inspection."""

import json
import re
import zipfile
from pathlib import Path

import pytest

from app.modules.library.corrupt_document import CorruptDocumentError
from app.modules.library.non_pdf_extraction import prepare_extraction, render_markdown
from app.modules.library.non_pdf_types import DeferredDocumentExtraction

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def shape(text, *, x=0, y=0, title=False, bullet=False):
    placeholder = '<p:ph type="title"/>' if title else ""
    props = '<a:pPr lvl="0"><a:buChar char="•"/></a:pPr>' if bullet else ""
    return f'<p:sp><p:nvSpPr><p:cNvPr id="1" name="Text"/><p:cNvSpPr/><p:nvPr>{placeholder}</p:nvPr></p:nvSpPr><p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="100" cy="100"/></a:xfrm></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p>{props}<a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>'


def deck(path: Path, bodies, *, hidden=(), extra=None):
    members = {}
    ids = "".join(f'<p:sldId id="{256 + i}" r:id="r{i}"/>' for i in range(len(bodies)))
    members["ppt/presentation.xml"] = (
        f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>'
    )
    rels = "".join(
        f'<Relationship Id="r{i}" Type="{R}/slide" Target="slides/slide{len(bodies) - i}.xml"/>'
        for i in range(len(bodies))
    )
    members["ppt/_rels/presentation.xml.rels"] = (
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>'
    )
    for i, body in enumerate(bodies):
        show = ' show="0"' if i in hidden else ""
        members[f"ppt/slides/slide{len(bodies) - i}.xml"] = (
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"{show}><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>'
        )
    members.update(extra or {})
    with zipfile.ZipFile(path, "w") as z:
        for name, content in members.items():
            z.writestr(name, content)
    return path


def prepare(path, tmp_path):
    return prepare_extraction(
        path,
        workspace=tmp_path / "work",
        mime_type="application/msword",
        source_path="wrong.doc",
    )


def test_native_text_order_groups_bullets_tables_and_hidden_notes(tmp_path):
    table = "<p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr><a:tc><a:txBody><a:p><a:r><a:t>Күзәнәк</a:t></a:r></a:p></a:txBody></a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    body = (
        shape("Ахыр", y=200)
        + shape("Исем", title=True)
        + f"<p:grpSp>{shape('Татар теле', y=100, bullet=True)}</p:grpSp>"
        + table
    )
    source = deck(
        tmp_path / "a.pptx",
        [body, shape("Икенче"), shape("Hidden")],
        hidden=(2,),
        extra={
            "ppt/notesSlides/notesSlide1.xml": "notes secret",
            "ppt/media/unused.png": b"not used",
        },
    )
    prepared = prepare(source, tmp_path)
    assert prepared.detected_format == "pptx"
    md = render_markdown(prepared, asset_urls={})
    assert (
        md.index("Исем")
        < md.index("Татар теле")
        < md.index("Ахыр")
        < md.index("Икенче")
    )
    assert "Күзәнәк" in md and "Hidden" not in md and "notes secret" not in md
    assert "-   Татар теле" in md
    report = json.loads((tmp_path / "work/pptx-inspection.json").read_text())
    assert report["slide_count"] == 3 and report["visible_slide_count"] == 2
    assert not report["reasons"]


@pytest.mark.parametrize(
    "visual,reason",
    [
        ("<p:pic/>", "pptx_slide_images"),
        ("<p:grpSp><p:pic/></p:grpSp>", "pptx_slide_images"),
        (
            '<p:sp><p:spPr><a:blipFill><a:blip r:link="external"/></a:blipFill></p:spPr></p:sp>',
            "pptx_slide_images",
        ),
        ("<p:bg><p:bgPr><a:blipFill/></p:bgPr></p:bg>", "pptx_slide_images"),
        (
            '<p:graphicFrame><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart"/></a:graphic></p:graphicFrame>',
            "pptx_unsupported_visuals",
        ),
        (
            '<p:graphicFrame><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/diagram"/></a:graphic></p:graphicFrame>',
            "pptx_unsupported_visuals",
        ),
        ("<p:oleObj/>", "pptx_unsupported_visuals"),
        (
            '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"/>',
            "pptx_unsupported_visuals",
        ),
        ("<p:pic/><p:oleObj/>", "pptx_slide_images"),
    ],
)
def test_visual_decks_defer_before_markdown(tmp_path, visual, reason):
    source = deck(tmp_path / "a.pptx", [shape("Native text"), visual])
    with pytest.raises(DeferredDocumentExtraction) as error:
        prepare(source, tmp_path)
    assert error.value.reason == reason and error.value.detected_format == "pptx"
    assert not (tmp_path / "work/final.md").exists()
    report = json.loads((tmp_path / "work/pptx-inspection.json").read_text())
    assert reason in report["reasons"]
    if "oleObj" in visual and "pic" in visual:
        assert len(report["reasons"]) == 2


def test_inherited_and_hidden_images_do_not_block(tmp_path):
    source = deck(
        tmp_path / "a.pptx",
        [shape("Body"), "<p:pic/>"],
        hidden=(1,),
        extra={
            "ppt/slideMasters/slideMaster1.xml": "<p:pic/>",
            "ppt/slideLayouts/slideLayout1.xml": "<p:pic/>",
        },
    )
    assert prepare(source, tmp_path).detected_format == "pptx"


def test_empty_deck_deferred(tmp_path):
    with pytest.raises(DeferredDocumentExtraction, match="pptx_no_text"):
        prepare(deck(tmp_path / "empty.pptx", [""]), tmp_path)


@pytest.mark.parametrize(
    "extra",
    [
        {"ppt/presentation.xml": "broken xml"},
        {"ppt/slides/slide1.xml": "broken xml"},
        {"ppt/_rels/presentation.xml.rels": "<Relationships/>"},
    ],
)
def test_malformed_packages_are_corruption(tmp_path, extra):
    with pytest.raises(CorruptDocumentError):
        prepare(deck(tmp_path / "bad.pptx", [shape("Text")], extra=extra), tmp_path)


def test_nested_bullets_and_numbering(tmp_path):
    body = shape("Outer", bullet=True)
    nested = '<a:p><a:pPr lvl="1"><a:buChar char="•"/></a:pPr><a:r><a:t>Inner</a:t></a:r></a:p>'
    numbered = '<a:p><a:pPr><a:buAutoNum type="arabicPeriod" startAt="3"/></a:pPr><a:r><a:t>Numbered</a:t></a:r></a:p>'
    body = body.replace("</p:txBody>", nested + numbered + "</p:txBody>")
    source = deck(tmp_path / "a.pptx", [body])
    md = render_markdown(prepare(source, tmp_path), asset_urls={})
    assert "\n    -   Inner" in md
    assert "3.  Numbered" in md or "3.   Numbered" in md


def test_group_coordinates_control_reading_order(tmp_path):
    group = (
        '<p:grpSp><p:grpSpPr><a:xfrm><a:off x="100" y="500"/><a:ext cx="200" cy="200"/><a:chOff x="0" y="0"/><a:chExt cx="100" cy="100"/></a:xfrm></p:grpSpPr>'
        + shape("Grouped", y=10)
        + "</p:grpSp>"
    )
    source = deck(tmp_path / "a.pptx", [group + shape("Earlier", y=400)])
    md = render_markdown(prepare(source, tmp_path), asset_urls={})
    assert md.index("Earlier") < md.index("Grouped")


def test_xml_limit_is_deferred_not_corrupted(tmp_path, monkeypatch):
    monkeypatch.setattr("app.modules.library.non_pdf_pptx.MAX_XML_BYTES", 10)
    with pytest.raises(DeferredDocumentExtraction, match="pptx_xml_limit"):
        prepare(deck(tmp_path / "a.pptx", [shape("Text")]), tmp_path)


def test_not_zip_pptx_is_corruption(tmp_path):
    source = tmp_path / "a.pptx"
    source.write_bytes(b"not a package")
    with pytest.raises(CorruptDocumentError, match="document_container"):
        prepare_extraction(
            source, workspace=tmp_path / "work", mime_type="", source_path="a.pptx"
        )


def test_symbol_only_deck_is_not_text(tmp_path):
    with pytest.raises(DeferredDocumentExtraction, match="pptx_no_text"):
        prepare(deck(tmp_path / "a.pptx", [shape("&amp;")]), tmp_path)


def test_source_punctuation_is_not_rewritten(tmp_path):
    text = "Татар теле — “үзгәртмә”; A–B; «сүз»; ..."
    md = render_markdown(
        prepare(deck(tmp_path / "a.pptx", [shape(text)]), tmp_path), asset_urls={}
    )
    assert text in re.sub(r"\\([^\w\s])", r"\1", md)


def test_empty_title_placeholder_does_not_create_heading(tmp_path):
    md = render_markdown(
        prepare(
            deck(tmp_path / "a.pptx", [shape("", title=True) + shape("Body")]), tmp_path
        ),
        asset_urls={},
    )
    assert not re.search(r"^##\s*$", md, re.MULTILINE)


def test_alternate_content_is_deferred_instead_of_silently_omitted(tmp_path):
    alternate = (
        '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><mc:Choice Requires="p">'
        + shape("Alternate text")
        + "</mc:Choice></mc:AlternateContent>"
    )
    with pytest.raises(DeferredDocumentExtraction, match="pptx_unsupported_visuals"):
        prepare(
            deck(tmp_path / "a.pptx", [shape("Ordinary text") + alternate]), tmp_path
        )


def test_table_rows_columns_and_merged_cells(tmp_path):
    def cell(text, attrs=""):
        return f"<a:tc {attrs}><a:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></a:txBody></a:tc>"

    table = (
        "<p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr>"
        + cell("Heading", 'gridSpan="2"')
        + cell("", 'hMerge="1"')
        + "</a:tr><a:tr>"
        + cell("Left")
        + cell("Right")
        + "</a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )
    md = render_markdown(
        prepare(deck(tmp_path / "a.pptx", [table]), tmp_path), asset_urls={}
    )
    assert md.index("Heading") < md.index("Left") < md.index("Right")
    assert 'colspan="2"' in md
