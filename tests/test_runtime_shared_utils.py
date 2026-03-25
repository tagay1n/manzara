"""Tests for shared runtime utility helpers used by embedded flows."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import app.modules.runtime_shared_utils as shared_utils


class _DirsLike(Enum):
    ENTRY = "sample_entry"


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


def test_library_and_maintenance_utils_share_common_functions() -> None:
    from app.modules.library.runtime import utils as library_utils
    from app.modules.maintenance.runtime import utils as maintenance_utils

    assert library_utils.get_in_workdir is maintenance_utils.get_in_workdir
    assert library_utils.read_config is maintenance_utils.read_config
    assert library_utils.load_upstream_metadata is maintenance_utils.load_upstream_metadata
