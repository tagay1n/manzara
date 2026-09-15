"""Library non pdf contracts coverage."""

from __future__ import annotations

from pathlib import Path

from app.modules.library.non_pdf_extraction import EXTRACTOR_VERSION
from app.modules.library.tasks import library_task_definitions


def test_task_catalog_includes_extract_non_pdf(tmp_path: Path) -> None:
    task = {
        item["task_id"]: item for item in library_task_definitions(app_root=tmp_path)
    }["library.extract_non_pdf"]

    assert task["panel_id"] == "library"
    assert task["title"] == "Extract non-pdf"
    assert "run_extract_non_pdf" in task["command"]["value"]
    assert "--per-mime-limit" not in task["command"]["value"]
    assert EXTRACTOR_VERSION == "nonpdf.v9"


def test_migration_allows_schema_without_external_document_catalog() -> None:
    migration = Path(
        "alembic/versions/20260825_0034_add_non_pdf_extraction_state.py"
    ).read_text(encoding="utf-8")

    assert "IF to_regclass" in migration
    assert "ADD CONSTRAINT fk_library_non_pdf_extraction_document" in migration
    create_table = migration.split("CREATE TABLE", 1)[1].split('"""', 1)[0]
    assert "FOREIGN KEY (md5)" not in create_table


def test_retry_policy_migration_defers_deterministic_existing_failures() -> None:
    migration = Path(
        "alembic/versions/20260826_0035_add_non_pdf_deferred_status.py"
    ).read_text(encoding="utf-8")

    assert "'deferred'" in migration
    assert "Extracted document contains only images; OCR required" in migration
    assert "Rendered Markdown validation failed" in migration
    assert "LibreOffice produced 0 DOCX files" in migration
    assert "couldn''t unpack docx container" in migration
