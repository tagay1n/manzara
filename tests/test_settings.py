"""Settings loader policy tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.settings import (
    _load_database_pool_size,
    _load_database_url,
    _load_local_state_path,
    normalize_database_url,
)


def test_local_state_path_defaults_under_artifacts_taxonomy(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MANZARA_LOCAL_STATE_PATH", raising=False)
    monkeypatch.delenv("MANZARA_CONFIG_PATH", raising=False)
    monkeypatch.setenv("MANZARA_ARTIFACTS_ROOT", str(tmp_path / "manzara"))
    assert _load_local_state_path() == tmp_path / "manzara" / "state" / "runtime.sqlite3"

    configured = tmp_path / "explicit.sqlite3"
    monkeypatch.setenv("MANZARA_LOCAL_STATE_PATH", str(configured))
    assert _load_local_state_path() == configured


def test_load_database_url_ignores_config_example(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MANZARA_DATABASE_URL", raising=False)
    monkeypatch.delenv("MANZARA_CONFIG_PATH", raising=False)

    (tmp_path / "config.example.yaml").write_text(
        yaml.safe_dump({"database_url": "postgresql://example/should_not_be_used"}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError):
        _load_database_url()


def test_aiven_postgres_url_is_normalized_for_sqlalchemy() -> None:
    assert normalize_database_url("postgres://user:pw@host/defaultdb?sslmode=require") == (
        "postgresql://user:pw@host/defaultdb?sslmode=require"
    )


def test_database_pool_size_defaults_conservatively_and_is_strict(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MANZARA_DB_POOL_SIZE", raising=False)
    assert _load_database_pool_size() == 4

    (tmp_path / "config.yaml").write_text("database_pool_size: 6\n", encoding="utf-8")
    assert _load_database_pool_size() == 6

    monkeypatch.setenv("MANZARA_DB_POOL_SIZE", "5")
    assert _load_database_pool_size() == 5

    for invalid in ("0", "9", "2.5", "many"):
        monkeypatch.setenv("MANZARA_DB_POOL_SIZE", invalid)
        with pytest.raises(RuntimeError, match="MANZARA_DB_POOL_SIZE"):
            _load_database_pool_size()
