from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

import scripts.backup_postgres_to_b2 as backup

NOW = datetime(2026, 9, 9, 1, 17, 23, tzinfo=timezone.utc)


def valid_environment() -> dict[str, str]:
    return {
        "MANZARA_DATABASE_URL": (
            "postgres://avnadmin:secret@db.example:12826/defaultdb?sslmode=require"
        ),
        "MANZARA_AIVEN_CA_CERT_BASE64": base64.b64encode(
            b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"
        ).decode("ascii"),
        "MANZARA_LOGICAL_BACKUP_S3_ENDPOINT": (
            "https://s3.eu-central-003.backblazeb2.com"
        ),
        "MANZARA_LOGICAL_BACKUP_S3_REGION": "eu-central-003",
        "MANZARA_LOGICAL_BACKUP_S3_BUCKET": "ttbackups",
        "MANZARA_LOGICAL_BACKUP_S3_ACCESS_KEY_ID": "key-id",
        "MANZARA_LOGICAL_BACKUP_S3_SECRET_ACCESS_KEY": "key-secret",
    }


def test_config_requires_https_storage_and_a_pem_ca() -> None:
    values = valid_environment()
    values["MANZARA_LOGICAL_BACKUP_S3_ENDPOINT"] = "http://storage.example"

    with pytest.raises(RuntimeError, match="must use HTTPS"):
        backup.BackupConfig.from_environment(values)

    values = valid_environment()
    values["MANZARA_AIVEN_CA_CERT_BASE64"] = base64.b64encode(b"not a CA").decode()
    with pytest.raises(RuntimeError, match="does not contain a PEM certificate"):
        backup.BackupConfig.from_environment(values)


def test_config_reports_every_missing_github_setting() -> None:
    values = valid_environment()
    for name in (
        "MANZARA_AIVEN_CA_CERT_BASE64",
        "MANZARA_LOGICAL_BACKUP_S3_ENDPOINT",
        "MANZARA_LOGICAL_BACKUP_S3_REGION",
    ):
        values.pop(name)

    with pytest.raises(RuntimeError) as captured:
        backup.BackupConfig.from_environment(values)

    message = str(captured.value)
    assert "MANZARA_AIVEN_CA_CERT_BASE64" in message
    assert "MANZARA_LOGICAL_BACKUP_S3_ENDPOINT" in message
    assert "MANZARA_LOGICAL_BACKUP_S3_REGION" in message


def test_backup_keys_use_independent_daily_and_monthly_tiers() -> None:
    keys = backup.backup_keys(NOW)

    assert keys.daily == (
        "logical/manzara/daily/2026/09/"
        "manzara-20260909T011723Z.dump"
    )
    assert keys.monthly == "logical/manzara/monthly/2026/manzara-2026-09.dump"


def test_service_entry_enforces_verified_tls_without_exposing_url() -> None:
    url = "postgres://avnadmin:secret@db.example:12826/defaultdb?sslmode=require"

    entry = backup.render_service_entry(
        url,
        container_ca_path="/backup/aiven-ca.pem",
    )
    commands = backup.postgres_commands(
        Path("/tmp/work"),
        image="postgres:18",
        user="1000:1000",
    )

    assert "password=secret" in entry
    assert "sslmode=verify-full" in entry
    assert "sslrootcert=/backup/aiven-ca.pem" in entry
    assert all(url not in argument for command in commands for argument in command)
    assert "--format=custom" in commands[0]
    assert "--schema=monocorpus" in commands[0]
    assert "--schema=public" in commands[0]
    assert "--extension=pg_trgm" in commands[0]
    assert commands[1][-2:] == ["--list", "/backup/manzara.dump"]


class FakeS3:
    def __init__(self, *, existing_monthly: dict | None = None) -> None:
        self.objects: dict[str, dict] = {}
        self.uploads: list[tuple[str, str, dict]] = []
        if existing_monthly is not None:
            self.objects[
                "logical/manzara/monthly/2026/manzara-2026-09.dump"
            ] = existing_monthly

    def upload_file(self, filename: str, bucket: str, key: str, ExtraArgs: dict) -> None:
        size = Path(filename).stat().st_size
        metadata = dict(ExtraArgs["Metadata"])
        self.uploads.append((bucket, key, ExtraArgs))
        self.objects[key] = {
            "ContentLength": size,
            "Metadata": metadata,
            "ServerSideEncryption": ExtraArgs["ServerSideEncryption"],
        }

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        del Bucket
        try:
            return self.objects[Key]
        except KeyError as exc:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadObject",
            ) from exc


def test_upload_creates_daily_and_first_monthly_with_sse_and_checksum(
    tmp_path: Path,
) -> None:
    dump = tmp_path / "manzara.dump"
    dump.write_bytes(b"valid custom dump")
    client = FakeS3()

    result = backup.upload_backup(
        client,
        bucket="ttbackups",
        dump_path=dump,
        now=NOW,
        git_sha="abc123",
    )

    assert [key for _, key, _ in client.uploads] == [
        "logical/manzara/daily/2026/09/manzara-20260909T011723Z.dump",
        "logical/manzara/monthly/2026/manzara-2026-09.dump",
    ]
    assert all(
        args["ServerSideEncryption"] == "AES256"
        for _, _, args in client.uploads
    )
    assert all(len(args["Metadata"]["sha256"]) == 64 for _, _, args in client.uploads)
    assert result.monthly_created is True


def test_upload_keeps_existing_monthly_recovery_point(tmp_path: Path) -> None:
    dump = tmp_path / "manzara.dump"
    dump.write_bytes(b"new daily dump")
    existing = {
        "ContentLength": 123,
        "Metadata": {"sha256": "a" * 64},
        "ServerSideEncryption": "AES256",
    }
    client = FakeS3(existing_monthly=existing)

    result = backup.upload_backup(
        client,
        bucket="ttbackups",
        dump_path=dump,
        now=NOW,
        git_sha="abc123",
    )

    assert len(client.uploads) == 1
    assert "/daily/" in client.uploads[0][1]
    assert result.monthly_created is False


def test_existing_monthly_without_sse_is_rejected(tmp_path: Path) -> None:
    dump = tmp_path / "manzara.dump"
    dump.write_bytes(b"new daily dump")
    client = FakeS3(
        existing_monthly={
            "ContentLength": 123,
            "Metadata": {"sha256": "a" * 64},
        }
    )

    with pytest.raises(RuntimeError, match="not encrypted with SSE-B2"):
        backup.upload_backup(
            client,
            bucket="ttbackups",
            dump_path=dump,
            now=NOW,
            git_sha="abc123",
        )
