#!/usr/bin/env python3
"""Install and validate the Backblaze pgBackRest repository configuration."""

from __future__ import annotations

import argparse
import grp
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urlsplit

from app.modules.maintenance.backup_s3_verify import load_backup_s3_settings


DEFAULT_CONFIG_TARGET = Path("/etc/pgbackrest/pgbackrest.conf")
DEFAULT_PG_PATH = Path("/var/lib/postgresql/18/main")


def _required(value: object, name: str) -> str:
    resolved = str(value or "").strip()
    if not resolved or "\n" in resolved or "\r" in resolved:
        raise ValueError(f"Invalid required value: {name}")
    return resolved


def _endpoint_host(endpoint_url: str) -> str:
    parsed = urlsplit(_required(endpoint_url, "endpoint_url"))
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("pgBackRest S3 endpoint must use HTTPS")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("pgBackRest S3 endpoint must not include a path or query")
    return parsed.hostname


def render_pgbackrest_config(
    *,
    stanza: str,
    pg_path: str,
    endpoint_url: str,
    region_name: str,
    bucket: str,
    repository_path: str,
    access_key_id: str,
    secret_access_key: str,
    cipher_pass: str,
    retention_full: int,
) -> str:
    """Render the single supported encrypted Backblaze repository config."""
    retention = int(retention_full)
    if retention < 1:
        raise ValueError("retention_full must be positive")
    lines = [
        f"[{_required(stanza, 'stanza')}]",
        f"pg1-path={_required(pg_path, 'pg_path')}",
        "",
        "[global]",
        "repo1-type=s3",
        f"repo1-path={_required(repository_path, 'repository_path')}",
        f"repo1-s3-bucket={_required(bucket, 'bucket')}",
        f"repo1-s3-endpoint={_endpoint_host(endpoint_url)}",
        f"repo1-s3-region={_required(region_name, 'region_name')}",
        "repo1-s3-uri-style=path",
        f"repo1-s3-key={_required(access_key_id, 'access_key_id')}",
        f"repo1-s3-key-secret={_required(secret_access_key, 'secret_access_key')}",
        "repo1-cipher-type=aes-256-cbc",
        f"repo1-cipher-pass={_required(cipher_pass, 'cipher_pass')}",
        f"repo1-retention-full={retention}",
        "start-fast=y",
        "",
    ]
    return "\n".join(lines)


def _redacted(text: str, secrets_to_mask: Sequence[str]) -> str:
    safe = str(text)
    for value in secrets_to_mask:
        if value:
            safe = safe.replace(value, "<redacted>")
    return safe


def _run_pgbackrest(
    config_path: Path,
    stanza: str,
    arguments: Sequence[str],
    *,
    secrets_to_mask: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    command = [
        "runuser",
        "--user",
        "postgres",
        "--",
        "pgbackrest",
        f"--config={config_path}",
        f"--stanza={stanza}",
        "--log-level-console=info",
        *arguments,
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.stdout:
        print(_redacted(result.stdout, secrets_to_mask), end="")
    if result.stderr:
        print(_redacted(result.stderr, secrets_to_mask), end="", file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"pgBackRest command failed with code {result.returncode}: {arguments[-1]}"
        )
    return result


def _latest_backup_label(
    config_path: Path,
    stanza: str,
    *,
    secrets_to_mask: Sequence[str],
) -> str:
    result = _run_pgbackrest(
        config_path,
        stanza,
        ["--output=json", "info"],
        secrets_to_mask=secrets_to_mask,
    )
    payload = json.loads(result.stdout)
    backups = payload[0].get("backup") or []
    if not backups:
        raise RuntimeError("pgBackRest repository contains no completed backups")
    return _required(backups[-1].get("label"), "latest backup label")


def install_and_backup(
    *,
    app_config_path: Path,
    target_path: Path = DEFAULT_CONFIG_TARGET,
    pg_path: Path = DEFAULT_PG_PATH,
    stanza: str = "monocorpus",
    retention_full: int = 60,
) -> tuple[Path, str]:
    """Cut over pgBackRest to Backblaze, rolling back config on failure."""
    if os.geteuid() != 0:
        raise PermissionError("Run this migration with sudo/root privileges")
    storage = load_backup_s3_settings(config_path=app_config_path)
    if not storage.get("ok"):
        raise RuntimeError(str(storage.get("error") or "backup storage is not configured"))
    if not pg_path.is_dir():
        raise FileNotFoundError(f"PostgreSQL data directory not found: {pg_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not target_path.exists():
        raise FileNotFoundError(f"Existing pgBackRest config not found: {target_path}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rollback_path = target_path.with_name(f"{target_path.name}.pre-backblaze-{timestamp}")
    shutil.copy2(target_path, rollback_path)

    cipher_pass = secrets.token_urlsafe(48)
    rendered = render_pgbackrest_config(
        stanza=stanza,
        pg_path=str(pg_path),
        endpoint_url=storage["endpoint_url"],
        region_name=storage["region_name"],
        bucket=storage["bucket"],
        repository_path=storage["repository_path"],
        access_key_id=storage["aws_access_key_id"],
        secret_access_key=storage["aws_secret_access_key"],
        cipher_pass=cipher_pass,
        retention_full=retention_full,
    )
    postgres_group = grp.getgrnam("postgres")
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=".pgbackrest-backblaze-",
        dir=target_path.parent,
        text=True,
    )
    staged_path = Path(temporary_name)
    secrets_to_mask = (
        storage["aws_access_key_id"],
        storage["aws_secret_access_key"],
        cipher_pass,
    )
    cutover_complete = False
    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(staged_path, 0, postgres_group.gr_gid)
        os.chmod(staged_path, 0o640)

        _run_pgbackrest(
            staged_path,
            stanza,
            ["stanza-create"],
            secrets_to_mask=secrets_to_mask,
        )
        os.replace(staged_path, target_path)
        cutover_complete = True
        os.chown(target_path, 0, postgres_group.gr_gid)
        os.chmod(target_path, 0o640)

        _run_pgbackrest(
            target_path,
            stanza,
            ["check"],
            secrets_to_mask=secrets_to_mask,
        )
        _run_pgbackrest(
            target_path,
            stanza,
            ["--type=full", "backup"],
            secrets_to_mask=secrets_to_mask,
        )
        label = _latest_backup_label(
            target_path,
            stanza,
            secrets_to_mask=secrets_to_mask,
        )
        _run_pgbackrest(
            target_path,
            stanza,
            [f"--set={label}", "--verbose", "verify"],
            secrets_to_mask=secrets_to_mask,
        )
        return rollback_path, label
    except Exception:
        if cutover_complete:
            shutil.copy2(rollback_path, target_path)
        raise
    finally:
        staged_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate the live pgBackRest repository to configured Backblaze B2 storage.",
    )
    parser.add_argument("--apply", action="store_true", help="Perform the live cutover and full backup.")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--target", type=Path, default=DEFAULT_CONFIG_TARGET)
    parser.add_argument("--pg-path", type=Path, default=DEFAULT_PG_PATH)
    parser.add_argument("--stanza", default="monocorpus")
    parser.add_argument("--retention-full", type=int, default=60)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.apply:
        print("Dry run only: pass --apply to install, check, back up, and verify.")
        return 0
    rollback_path, label = install_and_backup(
        app_config_path=args.config.resolve(),
        target_path=args.target,
        pg_path=args.pg_path,
        stanza=str(args.stanza),
        retention_full=args.retention_full,
    )
    print(f"Backblaze migration completed; backup_label={label}")
    print(f"Previous pgBackRest config retained at {rollback_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
