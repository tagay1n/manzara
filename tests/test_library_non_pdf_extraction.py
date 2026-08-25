"""Rich non-PDF detection and structured extraction coverage."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import zipfile

from PIL import Image

from app.modules.library.non_pdf_extraction import (
    PreparedExtraction,
    _collect_assets,
    detect_document_format,
    prepare_extraction,
    render_markdown,
)
from app.modules.library.non_pdf_repository import NonPdfExtractionRepository
from app.modules.library.runtime.run_extract_non_pdf import _write_content_archive
from app.modules.library.tasks import library_task_definitions


def test_task_catalog_includes_extract_non_pdf(tmp_path: Path) -> None:
    task = {
        item["task_id"]: item for item in library_task_definitions(app_root=tmp_path)
    }["library.extract_non_pdf"]

    assert task["panel_id"] == "library"
    assert task["title"] == "Extract non-pdf"
    assert "run_extract_non_pdf" in task["command"]["value"]


def test_migration_allows_schema_without_external_document_catalog() -> None:
    migration = Path(
        "alembic/versions/20260825_0034_add_non_pdf_extraction_state.py"
    ).read_text(encoding="utf-8")

    assert "IF to_regclass" in migration
    assert "ADD CONSTRAINT fk_library_non_pdf_extraction_document" in migration
    create_table = migration.split("CREATE TABLE", 1)[1].split('"""', 1)[0]
    assert "FOREIGN KEY (md5)" not in create_table


def test_detection_prefers_source_bytes_over_wrong_mime_and_extension(
    tmp_path: Path,
) -> None:
    rtf = tmp_path / "wrong.docx"
    rtf.write_bytes(b"{\\rtf1\\ansi Rich text}")
    pdf = tmp_path / "wrong.txt"
    pdf.write_bytes(b"%PDF-1.7\n")
    html = tmp_path / "wrong.doc"
    html.write_text("<!doctype html><html><body>Tatar</body></html>")

    assert detect_document_format(
        rtf, mime_type="application/msword", source_path="wrong.docx"
    ) == "rtf"
    assert detect_document_format(
        pdf, mime_type="text/plain", source_path="wrong.txt"
    ) == "pdf"
    assert detect_document_format(
        html, mime_type="application/msword", source_path="wrong.doc"
    ) == "html"


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
            "pandoc", "-f", "markdown", "-t", "docx", "--resource-path",
            str(tmp_path), "-o", str(docx),
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


def test_content_archive_contains_exact_md5_markdown_member(tmp_path: Path) -> None:
    md5 = "a" * 32
    archive_path = _write_content_archive(
        md5, "# Content\n", tmp_path / f"{md5}.zip"
    )

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [f"{md5}.md"]
        assert archive.read(f"{md5}.md") == b"# Content\n"
        assert archive.getinfo(f"{md5}.md").date_time == (1980, 1, 1, 0, 0, 0)


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
</FictionBook>""" % payload,
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
    assert json.loads(
        (tmp_path / "workspace-fb2" / "detection.json").read_text()
    )["detected_format"] == "fb2"


def test_unsupported_embedded_media_is_dropped_without_failing_document(
    tmp_path: Path, monkeypatch,
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
                        "c": [["", [], []], [{"t": "Str", "c": "Object"}], [str(media), ""]],
                    }
                ],
            },
        ],
    }

    def fail_conversion(*_args, **_kwargs):
        raise RuntimeError("unsupported image encoding")

    monkeypatch.setattr(
        "app.modules.library.non_pdf_extraction._browser_image", fail_conversion
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


class _Rows:
    def __init__(self, rows) -> None:  # noqa: ANN001
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Connection:
    def __init__(self, engine) -> None:  # noqa: ANN001
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, params):  # noqa: ANN001
        self.engine.sql = str(statement)
        self.engine.params = dict(params)
        return _Rows(self.engine.rows)


class _Engine:
    def __init__(self, rows=None) -> None:  # noqa: ANN001
        self.rows = list(rows or [])
        self.sql = ""
        self.params = {}

    def connect(self):
        return _Connection(self)


def test_candidate_queue_backfills_legacy_content_and_versions_unsupported() -> None:
    repository = NonPdfExtractionRepository.__new__(NonPdfExtractionRepository)
    repository.engine = _Engine()

    repository.list_candidates(extractor_version="nonpdf.v1", limit=10)

    assert "AND d.content_url IS NULL" not in repository.engine.sql
    assert "state.extractor_version IS DISTINCT FROM" in repository.engine.sql
    assert "state.status NOT IN ('ready', 'unsupported')" in repository.engine.sql
    assert "primary_storage_verified_at IS NOT NULL" in repository.engine.sql
    assert "CASE WHEN d.content_url IS NULL THEN 0 ELSE 1 END" in repository.engine.sql
