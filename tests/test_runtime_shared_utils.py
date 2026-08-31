"""Tests for shared runtime utility helpers used by embedded flows."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import app.modules.runtime_shared_utils as shared_utils


class _DirsLike(Enum):
    ENTRY = "sample_entry"


def test_default_workdir_is_under_manzara_root() -> None:
    assert shared_utils.workdir == "~/.manzara"


def test_contains_redacted_detects_nested_values() -> None:
    payload = {
        "a": "safe",
        "b": {"token": "abc<REDACTED>def"},
        "c": ["x", {"y": "z"}],
    }
    assert shared_utils._contains_redacted(payload) is True
    assert shared_utils._contains_redacted({"a": "safe"}) is False


def test_get_in_workdir_accepts_enum_like_values(tmp_path: Path) -> None:
    result = shared_utils.get_in_workdir(_DirsLike.ENTRY, file="item.txt", prefix=str(tmp_path))
    path = Path(result)
    assert path.parent.exists()
    assert path.name == "item.txt"
    assert str(path.parent).endswith("sample_entry")


def test_encrypt_decrypt_round_trip() -> None:
    # urlsafe_b64 key for 128-bit AES key bytes
    config = {"encryption_key": "MDEyMzQ1Njc4OWFiY2RlZg=="}
    source = "https://example.test/file.pdf"
    encrypted = shared_utils.encrypt(source, config)
    assert encrypted.startswith(shared_utils.prefix)
    assert shared_utils.decrypt(encrypted, config) == source


def test_get_engine_uses_manzara_schema_before_public(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setenv("MANZARA_DB_SCHEMA", "runtime_state")
    monkeypatch.setattr(
        shared_utils,
        "read_config",
        lambda: {"database_url": "postgresql://example.test/database"},
    )

    def create_engine(database_url, **kwargs):  # noqa: ANN001, ANN003
        captured.update(database_url=database_url, **kwargs)
        return object()

    monkeypatch.setattr(shared_utils, "create_engine", create_engine)

    shared_utils.get_engine()

    assert captured["connect_args"] == {
        "options": "-csearch_path=runtime_state,public"
    }


def test_library_utils_export_shared_common_functions() -> None:
    from app.modules.library.runtime import utils as library_utils

    assert set(library_utils.__all__) == set(shared_utils.__all__)
    assert callable(library_utils.get_in_workdir)
    assert callable(library_utils.read_config)
    assert not hasattr(library_utils, "load_upstream_metadata")
