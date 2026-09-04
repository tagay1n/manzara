"""Tests for the retention-oriented local storage layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.artifacts import (
    cache_dir,
    durable_dir,
    private_credentials_dir,
    task_runs_dir,
    workspace_dir,
)


def test_artifact_helpers_use_talkative_retention_areas(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "manzara"
    monkeypatch.setenv("MANZARA_ARTIFACTS_ROOT", str(root))

    assert cache_dir("source-documents") == root / "cache" / "source-documents"
    assert (
        workspace_dir("library", "metadata-extraction", run_id=17)
        == root / "workspaces" / "library" / "metadata-extraction" / "run-17"
    )
    assert durable_dir("database-migrations") == root / "durable" / "database-migrations"
    assert private_credentials_dir("google-drive") == (
        root / "private" / "credentials" / "google-drive"
    )
    assert task_runs_dir() == root / "logs" / "task-runs"

    guide = (root / "STORAGE_LAYOUT.txt").read_text(encoding="utf-8")
    assert "cache/" in guide
    assert "Safe to remove while tasks are idle" in guide
    assert "private/" in guide


@pytest.mark.parametrize("part", ["", ".", "..", "a/b", "a\\b"])
def test_artifact_helpers_reject_unsafe_path_parts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    part: str,
) -> None:
    monkeypatch.setenv("MANZARA_ARTIFACTS_ROOT", str(tmp_path))

    with pytest.raises(ValueError, match="artifact path part"):
        cache_dir(part)


def test_private_credential_directories_are_owner_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MANZARA_ARTIFACTS_ROOT", str(tmp_path / "manzara"))

    path = private_credentials_dir("database-migration")

    assert path.stat().st_mode & 0o777 == 0o700
