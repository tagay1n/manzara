"""Focused coverage for shared Shayan WebDAV contracts."""

from __future__ import annotations

import pytest

from app.modules.shayan.runtime.webdav import (
    NextcloudSettings,
    _normalize_remote_path,
    load_nextcloud_settings,
    temporary_remote_path,
)


def test_direct_upload_settings_require_only_hetzner_destination() -> None:
    settings = load_nextcloud_settings(
        {
            "nextcloud": {
                "webdav_url": "https://cloud.example/remote.php/dav/files/Admin",
                "username": "Admin",
                "password": "password",
                "shayan": {"shows": {"target_dir": "/Hetzner/Shows"}},
            }
        }
    )

    assert settings == NextcloudSettings(
        webdav_url="https://cloud.example/remote.php/dav/files/Admin",
        username="Admin",
        password="password",
        target_dirs={"shows": "/Hetzner/Shows"},
    )


def test_webdav_paths_are_safe_and_staging_name_is_deterministic() -> None:
    assert _normalize_remote_path("//Hetzner/Shows/") == "/Hetzner/Shows"
    assert temporary_remote_path("/Hetzner/Shows/episode.mp4", "ABC123") == (
        "/Hetzner/Shows/.manzara-abc123.uploading"
    )
    with pytest.raises(ValueError, match="Invalid WebDAV path"):
        _normalize_remote_path("/Hetzner/../Elsewhere")
