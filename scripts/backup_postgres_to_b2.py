"""Create, validate, and upload a portable Aiven PostgreSQL backup."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import boto3
from botocore.exceptions import BotoCoreError, ClientError

BACKUP_PREFIX = "logical/manzara"
DEFAULT_POSTGRES_IMAGE = "postgres:18"
CONTAINER_SERVICE_FILE = "/backup/service.conf"
CONTAINER_CA_FILE = "/backup/aiven-ca.pem"
CONTAINER_DUMP_FILE = "/backup/manzara.dump"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_ENVIRONMENT = (
    "MANZARA_DATABASE_URL",
    "MANZARA_AIVEN_CA_CERT_BASE64",
    "MANZARA_LOGICAL_BACKUP_S3_ENDPOINT",
    "MANZARA_LOGICAL_BACKUP_S3_REGION",
    "MANZARA_LOGICAL_BACKUP_S3_BUCKET",
    "MANZARA_LOGICAL_BACKUP_S3_ACCESS_KEY_ID",
    "MANZARA_LOGICAL_BACKUP_S3_SECRET_ACCESS_KEY",
)


@dataclass(frozen=True)
class BackupKeys:
    daily: str
    monthly: str


@dataclass(frozen=True)
class UploadResult:
    daily_key: str
    monthly_key: str
    monthly_created: bool
    size: int
    sha256: str


@dataclass(frozen=True)
class BackupConfig:
    database_url: str
    ca_certificate: bytes
    endpoint_url: str
    region_name: str
    bucket: str
    access_key_id: str
    secret_access_key: str

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> BackupConfig:
        values = os.environ if environ is None else environ

        missing = [
            name for name in REQUIRED_ENVIRONMENT if not str(values.get(name) or "").strip()
        ]
        if missing:
            raise RuntimeError(
                "Missing required backup settings: " + ", ".join(missing)
            )

        def required(name: str) -> str:
            return str(values[name]).strip()

        database_url = normalize_postgres_url(required("MANZARA_DATABASE_URL"))
        encoded_ca = required("MANZARA_AIVEN_CA_CERT_BASE64")
        try:
            ca_certificate = base64.b64decode(encoded_ca, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(
                "MANZARA_AIVEN_CA_CERT_BASE64 must be valid base64"
            ) from exc
        if b"BEGIN CERTIFICATE" not in ca_certificate:
            raise RuntimeError(
                "MANZARA_AIVEN_CA_CERT_BASE64 does not contain a PEM certificate"
            )

        endpoint_url = required("MANZARA_LOGICAL_BACKUP_S3_ENDPOINT")
        if not endpoint_url.startswith("https://"):
            raise RuntimeError("MANZARA_LOGICAL_BACKUP_S3_ENDPOINT must use HTTPS")

        return cls(
            database_url=database_url,
            ca_certificate=ca_certificate,
            endpoint_url=endpoint_url.rstrip("/"),
            region_name=required("MANZARA_LOGICAL_BACKUP_S3_REGION"),
            bucket=required("MANZARA_LOGICAL_BACKUP_S3_BUCKET"),
            access_key_id=required("MANZARA_LOGICAL_BACKUP_S3_ACCESS_KEY_ID"),
            secret_access_key=required(
                "MANZARA_LOGICAL_BACKUP_S3_SECRET_ACCESS_KEY"
            ),
        )


def normalize_postgres_url(value: str) -> str:
    """Normalize provider and SQLAlchemy schemes for libpq."""
    text = str(value or "").strip()
    for prefix in (
        "postgresql+psycopg2://",
        "postgresql+psycopg://",
        "postgres://",
    ):
        if text.startswith(prefix):
            text = "postgresql://" + text[len(prefix) :]
            break
    split = urlsplit(text)
    if (
        split.scheme != "postgresql"
        or not split.hostname
        or not split.path.strip("/")
        or not split.username
        or split.password is None
    ):
        raise ValueError(
            "MANZARA_DATABASE_URL must contain a PostgreSQL host, database, user, and password"
        )
    return text


def _safe_service_value(value: str, field: str) -> str:
    if any(character in value for character in ("\n", "\r")):
        raise ValueError(f"Invalid newline in PostgreSQL {field}")
    return value.replace("\\", "\\\\").replace("'", "\\'")


def render_service_entry(database_url: str, *, container_ca_path: str) -> str:
    """Render a libpq service entry that always verifies the Aiven certificate."""
    split = urlsplit(normalize_postgres_url(database_url))
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    values = {
        "host": split.hostname or "",
        "port": str(split.port or 5432),
        "dbname": unquote(split.path.lstrip("/")),
        "user": unquote(split.username or ""),
        "password": unquote(split.password or ""),
        "sslmode": "verify-full",
        "sslrootcert": container_ca_path,
        "connect_timeout": query.get("connect_timeout", "20"),
        "application_name": "manzara-github-logical-backup",
    }
    lines = ["[source]"]
    lines.extend(
        f"{key}={_safe_service_value(str(value), key)}"
        for key, value in values.items()
        if str(value)
    )
    return "\n".join(lines) + "\n"


def backup_keys(now: datetime) -> BackupKeys:
    if now.tzinfo is None:
        raise ValueError("Backup timestamp must be timezone-aware")
    utc = now.astimezone(timezone.utc)
    timestamp = utc.strftime("%Y%m%dT%H%M%SZ")
    return BackupKeys(
        daily=(
            f"{BACKUP_PREFIX}/daily/{utc:%Y/%m}/"
            f"manzara-{timestamp}.dump"
        ),
        monthly=f"{BACKUP_PREFIX}/monthly/{utc:%Y}/manzara-{utc:%Y-%m}.dump",
    )


def postgres_commands(
    workdir: Path,
    *,
    image: str = DEFAULT_POSTGRES_IMAGE,
    user: str,
) -> tuple[list[str], list[str]]:
    docker_prefix = [
        "docker",
        "run",
        "--rm",
        f"--user={user}",
        f"--volume={workdir.resolve()}:/backup:rw",
        f"--env=PGSERVICEFILE={CONTAINER_SERVICE_FILE}",
        image,
    ]
    dump = [
        *docker_prefix,
        "pg_dump",
        "--dbname=service=source",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--schema=monocorpus",
        "--schema=public",
        "--extension=pg_trgm",
        f"--file={CONTAINER_DUMP_FILE}",
    ]
    inspect = [
        *docker_prefix,
        "pg_restore",
        "--list",
        CONTAINER_DUMP_FILE,
    ]
    return dump, inspect


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise RuntimeError(detail[-4000:])
    return result


def create_dump(
    config: BackupConfig,
    workdir: Path,
    *,
    postgres_image: str = DEFAULT_POSTGRES_IMAGE,
) -> Path:
    workdir.mkdir(mode=0o700, parents=True, exist_ok=False)
    service_path = workdir / "service.conf"
    ca_path = workdir / "aiven-ca.pem"
    dump_path = workdir / "manzara.dump"
    service_path.write_text(
        render_service_entry(
            config.database_url,
            container_ca_path=CONTAINER_CA_FILE,
        ),
        encoding="utf-8",
    )
    ca_path.write_bytes(config.ca_certificate)
    private_mode = stat.S_IRUSR | stat.S_IWUSR
    service_path.chmod(private_mode)
    ca_path.chmod(private_mode)

    commands = postgres_commands(
        workdir,
        image=postgres_image,
        user=f"{os.getuid()}:{os.getgid()}",
    )
    _run(commands[0])
    if not dump_path.is_file() or dump_path.stat().st_size <= 0:
        raise RuntimeError("pg_dump completed without creating a non-empty archive")
    listing = _run(commands[1]).stdout
    if not listing.strip() or "TABLE DATA" not in listing:
        raise RuntimeError("pg_restore did not find table data in the archive")
    return dump_path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_not_found(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code") or "")
    return code in {"404", "NoSuchKey", "NotFound"}


def _validate_remote_object(
    head: Mapping[str, Any],
    *,
    label: str,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> None:
    if str(head.get("ServerSideEncryption") or "") != "AES256":
        raise RuntimeError(f"{label} is not encrypted with SSE-B2")
    size = int(head.get("ContentLength") or 0)
    if size <= 0 or (expected_size is not None and size != expected_size):
        raise RuntimeError(f"{label} has an unexpected remote size")
    checksum = str((head.get("Metadata") or {}).get("sha256") or "")
    if not _SHA256_RE.fullmatch(checksum):
        raise RuntimeError(f"{label} has no valid SHA-256 metadata")
    if expected_sha256 is not None and checksum != expected_sha256:
        raise RuntimeError(f"{label} SHA-256 metadata does not match the dump")


def _upload_one(
    client: Any,
    *,
    bucket: str,
    key: str,
    dump_path: Path,
    size: int,
    sha256: str,
    created_at: str,
    git_sha: str,
) -> None:
    metadata = {
        "sha256": sha256,
        "size": str(size),
        "created-at": created_at,
        "git-sha": git_sha or "unknown",
        "format": "pg_dump-custom",
        "schemas": "monocorpus,public",
    }
    client.upload_file(
        str(dump_path),
        bucket,
        key,
        ExtraArgs={
            "ServerSideEncryption": "AES256",
            "Metadata": metadata,
        },
    )
    head = client.head_object(Bucket=bucket, Key=key)
    _validate_remote_object(
        head,
        label=key,
        expected_size=size,
        expected_sha256=sha256,
    )


def upload_backup(
    client: Any,
    *,
    bucket: str,
    dump_path: Path,
    now: datetime,
    git_sha: str,
) -> UploadResult:
    keys = backup_keys(now)
    size = dump_path.stat().st_size
    if size <= 0:
        raise RuntimeError("Refusing to upload an empty PostgreSQL dump")
    checksum = _sha256(dump_path)
    created_at = now.astimezone(timezone.utc).isoformat()

    _upload_one(
        client,
        bucket=bucket,
        key=keys.daily,
        dump_path=dump_path,
        size=size,
        sha256=checksum,
        created_at=created_at,
        git_sha=git_sha,
    )

    monthly_created = False
    try:
        existing_monthly = client.head_object(Bucket=bucket, Key=keys.monthly)
    except ClientError as exc:
        if not _is_not_found(exc):
            raise
        _upload_one(
            client,
            bucket=bucket,
            key=keys.monthly,
            dump_path=dump_path,
            size=size,
            sha256=checksum,
            created_at=created_at,
            git_sha=git_sha,
        )
        monthly_created = True
    else:
        _validate_remote_object(existing_monthly, label=keys.monthly)

    return UploadResult(
        daily_key=keys.daily,
        monthly_key=keys.monthly,
        monthly_created=monthly_created,
        size=size,
        sha256=checksum,
    )


def _s3_client(config: BackupConfig) -> Any:
    return boto3.client(
        "s3",
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        endpoint_url=config.endpoint_url,
        region_name=config.region_name,
    )


def _write_github_summary(result: UploadResult) -> None:
    summary_path = str(os.environ.get("GITHUB_STEP_SUMMARY") or "").strip()
    if not summary_path:
        return
    monthly_status = "created" if result.monthly_created else "already present"
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write("## PostgreSQL logical backup\n\n")
        handle.write(f"- Daily object: `{result.daily_key}`\n")
        handle.write(f"- Monthly object: `{result.monthly_key}` ({monthly_status})\n")
        handle.write(f"- Size: `{result.size}` bytes\n")
        handle.write(f"- SHA-256: `{result.sha256}`\n")
        handle.write("- Storage encryption: `AES256 (SSE-B2)`\n")


def run_backup(*, postgres_image: str = DEFAULT_POSTGRES_IMAGE) -> UploadResult:
    config = BackupConfig.from_environment()
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="manzara-logical-backup-") as parent:
        dump_path = create_dump(
            config,
            Path(parent) / "work",
            postgres_image=postgres_image,
        )
        result = upload_backup(
            _s3_client(config),
            bucket=config.bucket,
            dump_path=dump_path,
            now=now,
            git_sha=str(os.environ.get("GITHUB_SHA") or ""),
        )
    _write_github_summary(result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-image", default=DEFAULT_POSTGRES_IMAGE)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run_backup(postgres_image=args.postgres_image)
    except (BotoCoreError, ClientError, OSError, RuntimeError, ValueError) as exc:
        print(f"::error::PostgreSQL logical backup failed: {exc}", flush=True)
        return 1
    print(json.dumps(result.__dict__, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
