#!/usr/bin/env python3
"""Restore a pgBackRest backup into an isolated PostgreSQL drill cluster."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone


DEFAULT_STANZA = "monocorpus"
DEFAULT_PARENT = Path("/var/lib/postgresql/18")
DEFAULT_PORT = 55432
POSTGRES_BIN_DIR = Path("/usr/lib/postgresql/18/bin")


def render_drill_postgresql_conf(
    *,
    data_path: str,
    socket_path: str,
    port: int,
) -> str:
    """Return the minimal config required to start an isolated restored cluster."""
    safe_port = int(port)
    if not 1024 <= safe_port <= 65535:
        raise ValueError("restore drill port must be between 1024 and 65535")
    return "\n".join(
        (
            f"data_directory = '{data_path}'",
            "listen_addresses = ''",
            f"port = {safe_port}",
            f"unix_socket_directories = '{socket_path}'",
            "archive_mode = off",
            "max_connections = 20",
            "shared_buffers = '128MB'",
            "logging_collector = off",
            "",
        )
    )


def _run_as_postgres(arguments: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["runuser", "--user", "postgres", "--", *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _run_checked(arguments: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    result = _run_as_postgres(arguments, timeout=timeout)
    if result.returncode == 0:
        return result
    message = (result.stderr or result.stdout or "pgBackRest/PostgreSQL command failed").strip()
    raise RuntimeError(message)


def _latest_backup_label(stanza: str) -> str:
    result = _run_checked(
        ["pgbackrest", f"--stanza={stanza}", "--output=json", "info"],
    )
    payload = json.loads(result.stdout)
    backups = payload[0].get("backup") or []
    if not backups:
        raise RuntimeError("No completed pgBackRest backup is available for the drill")
    return str(backups[-1]["label"])


def _chown_postgres(path: Path) -> None:
    account = pwd.getpwnam("postgres")
    os.chown(path, account.pw_uid, account.pw_gid)


def run_restore_drill(
    *,
    stanza: str = DEFAULT_STANZA,
    backup_label: str | None = None,
    parent: Path = DEFAULT_PARENT,
    port: int = DEFAULT_PORT,
    keep: bool = False,
) -> str:
    """Restore, start, query, stop, and remove an isolated backup drill cluster."""
    if os.geteuid() != 0:
        raise PermissionError("Run the restore drill with sudo/root privileges")
    if not parent.is_dir() or parent != DEFAULT_PARENT:
        raise ValueError(f"Restore drill parent must be the existing {DEFAULT_PARENT}")
    if not (POSTGRES_BIN_DIR / "pg_ctl").is_file():
        raise FileNotFoundError("PostgreSQL 18 pg_ctl is unavailable")

    label = backup_label or _latest_backup_label(stanza)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    drill_root = Path(tempfile.mkdtemp(prefix=f"restore-drill-{timestamp}-", dir=parent))
    data_path = drill_root / "data"
    socket_path = Path(tempfile.mkdtemp(prefix=f"manzara-restore-{timestamp}-", dir="/tmp"))
    _chown_postgres(drill_root)
    _chown_postgres(socket_path)
    started = False
    try:
        _run_checked(
            [
                "pgbackrest",
                f"--stanza={stanza}",
                f"--set={label}",
                f"--pg1-path={data_path}",
                "--log-level-console=warn",
                "restore",
            ],
            timeout=1800,
        )
        config_path = data_path / "postgresql.conf"
        config_path.write_text(
            render_drill_postgresql_conf(
                data_path=str(data_path),
                socket_path=str(socket_path),
                port=port,
            ),
            encoding="utf-8",
        )
        hba_path = data_path / "pg_hba.conf"
        hba_path.write_text("local all all trust\n", encoding="utf-8")
        _chown_postgres(config_path)
        _chown_postgres(hba_path)

        _run_checked(
            [
                str(POSTGRES_BIN_DIR / "pg_ctl"),
                "-D",
                str(data_path),
                "-o",
                f"-c config_file={config_path} -c hba_file={hba_path}",
                "-w",
                "-t",
                "180",
                "start",
            ],
            timeout=240,
        )
        started = True
        query = (
            "select current_setting('server_version_num'), pg_is_in_recovery(), "
            "(select count(*) from information_schema.tables "
            "where table_schema not in ('pg_catalog', 'information_schema'))"
        )
        result = _run_checked(
            [
                str(POSTGRES_BIN_DIR / "psql"),
                "-h",
                str(socket_path),
                "-p",
                str(port),
                "-d",
                "monocorpus",
                "-Atqc",
                query,
            ],
        )
        details = result.stdout.strip()
        if not details:
            raise RuntimeError("Restore drill query returned no result")
        print(f"Restore drill passed for backup {label}: {details}")
        return details
    finally:
        if started:
            _run_as_postgres(
                [str(POSTGRES_BIN_DIR / "pg_ctl"), "-D", str(data_path), "-m", "fast", "stop"],
                timeout=180,
            )
        if keep:
            print(f"Restore drill files retained at {drill_root}")
            print(f"Restore drill socket directory retained at {socket_path}")
        else:
            shutil.rmtree(drill_root, ignore_errors=True)
            shutil.rmtree(socket_path, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an isolated pgBackRest restore drill.")
    parser.add_argument("--stanza", default=DEFAULT_STANZA)
    parser.add_argument("--set", dest="backup_label", default=None)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    run_restore_drill(
        stanza=str(args.stanza),
        backup_label=args.backup_label,
        port=args.port,
        keep=bool(args.keep),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
