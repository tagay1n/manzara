"""Regression tests for library runtime script entrypoints."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.modules.library.runtime import run_collection_validate


@pytest.mark.parametrize(
    "relative_script_path",
    [
        "app/modules/library/runtime/run_collection_detect.py",
        "app/modules/library/runtime/run_collection_validate.py",
        "app/modules/library/runtime/run_collection_apply.py",
        "app/modules/library/runtime/run_normalization_refresh.py",
        "app/modules/library/runtime/run_generate_book_previews.py",
        "app/modules/library/runtime/run_metadata_extract.py",
        "app/modules/library/runtime/run_extract_non_pdf.py",
        "app/modules/maintenance/runtime/migrate_pdf_content.py",
    ],
)
def test_library_runtime_script_help_runs_outside_repo_cwd(
    relative_script_path: str,
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / relative_script_path
    result = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"
    assert "usage:" in result.stdout.lower()


def test_collection_runtime_events_use_collections_panel() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    detect_source = (
        repo_root / "app/modules/library/runtime/run_collection_detect.py"
    ).read_text()
    validation_source = (
        repo_root / "app/modules/library/collection_validation.py"
    ).read_text()
    catalog_source = (
        repo_root / "app/modules/library/collection_catalog.py"
    ).read_text()

    assert "panel_id=COLLECTIONS_PANEL_ID" in detect_source
    assert "PANEL_ID = COLLECTIONS_PANEL_ID" in validation_source
    assert "panel_id=COLLECTIONS_PANEL_ID" in catalog_source


def test_collection_excerpt_uses_shared_manzara_workdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "a" * 32
    content_dir = tmp_path / "cache" / "extracted-document-content"
    content_dir.mkdir(parents=True)
    with zipfile.ZipFile(content_dir / f"{digest}.zip", "w") as archive:
        archive.writestr("document.md", "First line\n\nSecond line")
    monkeypatch.setattr(run_collection_validate, "workdir", str(tmp_path))

    assert run_collection_validate._excerpt(digest) == "First line\nSecond line"
