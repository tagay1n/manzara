"""Focused tests for the pgBackRest Backblaze migration helper."""

from __future__ import annotations

import pytest

from scripts.migrate_pgbackrest_to_backblaze import render_pgbackrest_config


def test_render_pgbackrest_config_targets_one_encrypted_backblaze_repository() -> None:
    rendered = render_pgbackrest_config(
        stanza="monocorpus",
        pg_path="/var/lib/postgresql/18/main",
        endpoint_url="https://s3.eu-central-003.backblazeb2.com",
        region_name="eu-central-003",
        bucket="ttbackups",
        repository_path="/pgbackrest",
        access_key_id="key-id",
        secret_access_key="key-secret",
        cipher_pass="cipher-secret",
        retention_full=60,
    )

    assert "[monocorpus]" in rendered
    assert "pg1-path=/var/lib/postgresql/18/main" in rendered
    assert "repo1-s3-endpoint=s3.eu-central-003.backblazeb2.com" in rendered
    assert "repo1-s3-uri-style=path" in rendered
    assert "repo1-s3-bucket=ttbackups" in rendered
    assert "repo1-path=/pgbackrest" in rendered
    assert "repo1-cipher-type=aes-256-cbc" in rendered
    assert "repo1-cipher-pass=cipher-secret" in rendered
    assert "repo1-retention-full=60" in rendered
    assert "https://" not in rendered


def test_render_pgbackrest_config_rejects_non_https_endpoint() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        render_pgbackrest_config(
            stanza="monocorpus",
            pg_path="/var/lib/postgresql/18/main",
            endpoint_url="http://s3.example.test",
            region_name="region",
            bucket="ttbackups",
            repository_path="/pgbackrest",
            access_key_id="key-id",
            secret_access_key="key-secret",
            cipher_pass="cipher-secret",
            retention_full=60,
        )
