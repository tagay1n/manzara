"""Settings loader policy tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.settings import (
    _load_database_url,
    _load_postgres_backup_mode,
    normalize_database_url,
    task_is_available,
)


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


def test_postgres_backup_mode_is_strict(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MANZARA_POSTGRES_BACKUP_MODE", raising=False)
    assert _load_postgres_backup_mode() == "local_pgbackrest"

    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"postgres_backup_mode": "managed"}),
        encoding="utf-8",
    )
    assert _load_postgres_backup_mode() == "managed"

    monkeypatch.setenv("MANZARA_POSTGRES_BACKUP_MODE", "managed")
    assert _load_postgres_backup_mode() == "managed"

    monkeypatch.setenv("MANZARA_POSTGRES_BACKUP_MODE", "cloudish")
    with pytest.raises(RuntimeError, match="MANZARA_POSTGRES_BACKUP_MODE"):
        _load_postgres_backup_mode()


def test_aiven_postgres_url_is_normalized_for_sqlalchemy() -> None:
    assert normalize_database_url("postgres://user:pw@host/defaultdb?sslmode=require") == (
        "postgresql://user:pw@host/defaultdb?sslmode=require"
    )


def test_managed_mode_disables_only_local_pgbackrest_tasks() -> None:
    settings = type("Settings", (), {"postgres_backup_mode": "managed"})()

    assert not task_is_available(settings, "maintenance.pgbackrest_backup_full")
    assert not task_is_available(settings, "maintenance.pgbackrest_backup_incr")
    assert task_is_available(settings, "maintenance.dump_state")
