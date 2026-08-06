"""Regression tests for library runtime script entrypoints."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "relative_script_path",
    [
        "app/modules/library/runtime/run_collection_detect.py",
        "app/modules/library/runtime/run_collection_validate.py",
        "app/modules/library/runtime/run_collection_apply.py",
        "app/modules/library/runtime/run_normalization_refresh.py",
        "app/modules/library/runtime/run_generate_book_previews.py",
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
