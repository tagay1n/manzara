"""Settings loader policy tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.settings import _load_database_url


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
