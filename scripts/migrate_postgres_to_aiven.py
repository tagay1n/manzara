#!/usr/bin/env python3
"""Guarded filtered migration from local PostgreSQL to Aiven."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, unquote, urlsplit

import psycopg2

from app.artifacts import artifacts_root


SOURCE_ENV = "MANZARA_SOURCE_DATABASE_URL"
TARGET_ENV = "MANZARA_TARGET_DATABASE_URL"
PRODUCTION_SCHEMAS = ("monocorpus", "public")
EXCLUDED_TABLE_DATA = frozenset({"monocorpus.events", "monocorpus.run_logs"})
EXCLUDED_EVENT_TYPES = ("task.log", "task.progress")
DEFAULT_MAX_BYTES = 750 * 1024 * 1024


@dataclass(frozen=True)
class DatabaseSnapshot:
    host: str
    port: int
    database: str
    server_version_num: int
    selected_bytes: int
    active_runs: int
    table_counts: dict[str, int]
    alembic_versions: dict[str, str]


def normalize_postgres_url(value: str) -> str:
    """Normalize accepted PostgreSQL URL schemes for libpq and SQLAlchemy."""
    text = str(value or "").strip()
    replacements = (
        ("postgresql+psycopg2://", "postgresql://"),
        ("postgresql+psycopg://", "postgresql://"),
        ("postgres://", "postgresql://"),
    )
    for prefix, replacement in replacements:
        if text.startswith(prefix):
            text = replacement + text[len(prefix) :]
            break
    split = urlsplit(text)
    if split.scheme != "postgresql" or not split.hostname or not split.path.strip("/"):
        raise ValueError("Expected a PostgreSQL URL with host and database name")
    return text


def _required_url(env_name: str) -> str:
    value = str(os.environ.get(env_name) or "").strip()
    if not value:
        raise RuntimeError(f"{env_name} is required")
    return normalize_postgres_url(value)


def _safe_value(value: str, field: str) -> str:
    if any(character in value for character in ("\n", "\r")):
        raise ValueError(f"Invalid newline in PostgreSQL {field}")
    return value.replace("\\", "\\\\").replace("'", "\\'")


def render_service_entry(name: str, database_url: str) -> str:
    """Render one private libpq service entry; callers must protect the file."""
    split = urlsplit(normalize_postgres_url(database_url))
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    values = {
        "host": split.hostname or "",
        "port": str(split.port or 5432),
        "dbname": unquote(split.path.lstrip("/")),
        "user": unquote(split.username or ""),
        "password": unquote(split.password or ""),
    }
    for key in ("sslmode", "sslrootcert", "connect_timeout", "application_name"):
        if query.get(key):
            values[key] = query[key]
    lines = [f"[{_safe_value(name, 'service name')}]"]
    lines.extend(
        f"{key}={_safe_value(str(value), key)}"
        for key, value in values.items()
        if str(value)
    )
    return "\n".join(lines) + "\n"


@contextmanager
def private_service_file(source_url: str, target_url: str) -> Iterator[Path]:
    """Create a temporary 0600 libpq service file and remove it afterward."""
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="manzara-pg-service-",
        suffix=".conf",
        delete=False,
    )
    path = Path(handle.name)
    try:
        handle.write(render_service_entry("source", source_url))
        handle.write(render_service_entry("target", target_url))
        handle.close()
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def filter_restore_toc(value: str) -> str:
    """Disable only creation of the target's existing public schema."""
    lines: list[str] = []
    for line in str(value).splitlines():
        if " SCHEMA - public " in f" {line} " and not line.lstrip().startswith(";"):
            lines.append(";" + line)
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def _connect(database_url: str):
    return psycopg2.connect(normalize_postgres_url(database_url))


def _table_names(cursor: Any) -> list[str]:
    cursor.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_type = 'BASE TABLE'
          AND table_schema = ANY(%s)
        ORDER BY table_schema, table_name
        """,
        (list(PRODUCTION_SCHEMAS),),
    )
    return [f"{schema}.{table}" for schema, table in cursor.fetchall()]


def _table_count(cursor: Any, qualified_name: str) -> int:
    schema, table = qualified_name.split(".", 1)
    cursor.execute(
        "SELECT COUNT(*) FROM {}.{}".format(
            psycopg2.extensions.quote_ident(schema, cursor),
            psycopg2.extensions.quote_ident(table, cursor),
        )
    )
    return int(cursor.fetchone()[0])


def _alembic_versions(cursor: Any) -> dict[str, str]:
    versions: dict[str, str] = {}
    for schema, table in (
        ("monocorpus", "alembic_version_manzara"),
        ("public", "alembic_version"),
    ):
        cursor.execute(
            """
            SELECT to_regclass(%s)
            """,
            (f'"{schema}"."{table}"',),
        )
        if cursor.fetchone()[0] is None:
            continue
        cursor.execute(
            "SELECT version_num FROM {}.{}".format(
                psycopg2.extensions.quote_ident(schema, cursor),
                psycopg2.extensions.quote_ident(table, cursor),
            )
        )
        row = cursor.fetchone()
        if row:
            versions[f"{schema}.{table}"] = str(row[0])
    return versions


def inspect_database(database_url: str, *, source: bool) -> DatabaseSnapshot:
    """Collect the non-secret state used to gate and verify migration."""
    split = urlsplit(normalize_postgres_url(database_url))
    conn = _connect(database_url)
    conn.set_session(readonly=True, autocommit=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SHOW server_version_num")
            version = int(cursor.fetchone()[0])
            names = _table_names(cursor)
            counts = {
                name: _table_count(cursor, name)
                for name in names
                if not source or name not in EXCLUDED_TABLE_DATA
            }
            if source:
                cursor.execute(
                    """
                    SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0)
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relkind IN ('r', 'm')
                      AND n.nspname = ANY(%s)
                      AND (n.nspname || '.' || c.relname) <> ALL(%s)
                    """,
                    (list(PRODUCTION_SCHEMAS), list(EXCLUDED_TABLE_DATA)),
                )
                selected_bytes = int(cursor.fetchone()[0])
                active_runs = 0
                if "monocorpus.runs" in names:
                    cursor.execute(
                        """
                        SELECT COUNT(*) FROM monocorpus.runs
                        WHERE status IN (
                            'starting', 'running', 'stopping_graceful', 'stopping_force'
                        )
                        """
                    )
                    active_runs = int(cursor.fetchone()[0])
            else:
                cursor.execute("SELECT pg_database_size(current_database())")
                selected_bytes = int(cursor.fetchone()[0])
                active_runs = 0
            versions = _alembic_versions(cursor)
    finally:
        conn.close()
    return DatabaseSnapshot(
        host=str(split.hostname or ""),
        port=int(split.port or 5432),
        database=unquote(split.path.lstrip("/")),
        server_version_num=version,
        selected_bytes=selected_bytes,
        active_runs=active_runs,
        table_counts=counts,
        alembic_versions=versions,
    )


def _target_has_pg_trgm(database_url: str) -> bool:
    conn = _connect(database_url)
    conn.set_session(readonly=True, autocommit=True)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm')"
            )
            return bool(cursor.fetchone()[0])
    finally:
        conn.close()


def preflight(source_url: str, target_url: str, max_bytes: int) -> tuple[DatabaseSnapshot, DatabaseSnapshot]:
    source = inspect_database(source_url, source=True)
    target = inspect_database(target_url, source=False)
    if (source.host, source.port, source.database) == (
        target.host,
        target.port,
        target.database,
    ):
        raise RuntimeError("Source and target resolve to the same database")
    if source.active_runs:
        raise RuntimeError(f"Source has {source.active_runs} active run(s)")
    if source.selected_bytes > int(max_bytes):
        raise RuntimeError(
            f"Selected source relations use {source.selected_bytes} bytes; limit is {int(max_bytes)}"
        )
    if target.table_counts:
        raise RuntimeError("Target already contains production-schema tables")
    source_major = source.server_version_num // 10000
    target_major = target.server_version_num // 10000
    if target_major < source_major:
        raise RuntimeError("Target PostgreSQL major version is older than the source")
    if not _target_has_pg_trgm(target_url):
        raise RuntimeError("Target does not offer the required pg_trgm extension")
    return source, target


def _run(command: list[str], service_file: Path, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PGSERVICEFILE"] = str(service_file)
    return subprocess.run(command, env=env, check=True, text=True, **kwargs)


def restore_dump(service_file: Path, dumpdir: Path, restore_list: Path) -> None:
    """Restore in low-WAL-pressure phases suitable for Aiven's free tier."""
    for section in ("pre-data", "data", "post-data"):
        _run(
            [
                "pg_restore",
                "--dbname=service=target",
                f"--section={section}",
                "--jobs=1",
                "--no-owner",
                "--no-privileges",
                "--no-comments",
                "--exit-on-error",
                f"--use-list={restore_list}",
                str(dumpdir),
            ],
            service_file,
        )


def _copy_selected_events(source_url: str, target_url: str, destination: Path) -> int:
    source = _connect(source_url)
    target = _connect(target_url)
    try:
        with destination.open("w+", encoding="utf-8", newline="") as output:
            with source.cursor() as cursor:
                cursor.copy_expert(
                    """
                    COPY (
                        SELECT event_id, type, task_id, run_id, panel_id, ts, payload_json
                        FROM monocorpus.events
                        WHERE type <> ALL(ARRAY['task.log', 'task.progress'])
                        ORDER BY event_id
                    ) TO STDOUT WITH (FORMAT CSV)
                    """,
                    output,
                )
            output.flush()
            output.seek(0)
            with target.cursor() as cursor:
                cursor.copy_expert(
                    """
                    COPY monocorpus.events (
                        event_id, type, task_id, run_id, panel_id, ts, payload_json
                    ) FROM STDIN WITH (FORMAT CSV)
                    """,
                    output,
                )
                cursor.execute(
                    "SELECT COALESCE(MAX(event_id), 0) FROM monocorpus.events"
                )
                copied_max = int(cursor.fetchone()[0])
            with source.cursor() as cursor:
                cursor.execute("SELECT COALESCE(MAX(event_id), 0) FROM monocorpus.events")
                source_max = int(cursor.fetchone()[0])
            with target.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT setval(
                        pg_get_serial_sequence('monocorpus.events', 'event_id'),
                        %s,
                        %s
                    )
                    """,
                    (max(source_max, copied_max, 1), max(source_max, copied_max) > 0),
                )
        target.commit()
        source.rollback()
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()
    with destination.open("r", encoding="utf-8", newline="") as handle:
        return sum(1 for _ in csv.reader(handle))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_migration(
    source_url: str,
    target_url: str,
    *,
    migration_root: Path,
    max_bytes: int,
) -> Path:
    source_before, target_before = preflight(source_url, target_url, max_bytes)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    workdir = Path(migration_root) / timestamp
    dumpdir = workdir / "dump"
    workdir.mkdir(parents=True, exist_ok=False)

    with private_service_file(source_url, target_url) as service_file:
        _run(
            [
                "pg_dump",
                "--dbname=service=source",
                "--format=directory",
                "--jobs=4",
                "--no-owner",
                "--no-privileges",
                "--schema=monocorpus",
                "--schema=public",
                "--extension=pg_trgm",
                "--exclude-table-data=monocorpus.events",
                "--exclude-table-data=monocorpus.run_logs",
                f"--file={dumpdir}",
            ],
            service_file,
        )
        listed = _run(
            ["pg_restore", "--list", str(dumpdir)],
            service_file,
            capture_output=True,
        ).stdout
        restore_list = workdir / "restore.list"
        restore_list.write_text(filter_restore_toc(listed), encoding="utf-8")
        restore_dump(service_file, dumpdir, restore_list)

    events_path = workdir / "retained-events.csv"
    retained_events = _copy_selected_events(source_url, target_url, events_path)
    target_conn = _connect(target_url)
    try:
        target_conn.autocommit = True
        with target_conn.cursor() as cursor:
            cursor.execute("ANALYZE")
    finally:
        target_conn.close()

    source_after = inspect_database(source_url, source=True)
    target_after = inspect_database(target_url, source=False)
    expected_counts = dict(source_after.table_counts)
    expected_counts["monocorpus.events"] = retained_events
    expected_counts["monocorpus.run_logs"] = 0
    mismatches = {
        name: {"source": expected, "target": target_after.table_counts.get(name)}
        for name, expected in expected_counts.items()
        if target_after.table_counts.get(name) != expected
    }
    if mismatches:
        raise RuntimeError(f"Post-restore row-count mismatches: {mismatches}")
    if target_after.selected_bytes > int(max_bytes):
        raise RuntimeError("Restored target exceeds the configured storage safety limit")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "excluded_table_data": sorted(EXCLUDED_TABLE_DATA),
        "excluded_event_types": list(EXCLUDED_EVENT_TYPES),
        "retained_events": retained_events,
        "retained_events_sha256": _sha256(events_path),
        "source_before": asdict(source_before),
        "source_after": asdict(source_after),
        "target_before": asdict(target_before),
        "target_after": asdict(target_after),
    }
    manifest_path = workdir / "migration-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--migration-root",
        type=Path,
        default=None,
        help="Default: ~/.manzara/migrations",
    )
    parser.add_argument("--max-target-bytes", type=int, default=DEFAULT_MAX_BYTES)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source_url = _required_url(SOURCE_ENV)
    target_url = _required_url(TARGET_ENV)
    if args.preflight:
        source, target = preflight(source_url, target_url, args.max_target_bytes)
        print(
            json.dumps(
                {"ok": True, "source": asdict(source), "target": asdict(target)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    root = args.migration_root or (artifacts_root() / "migrations")
    manifest = apply_migration(
        source_url,
        target_url,
        migration_root=root,
        max_bytes=args.max_target_bytes,
    )
    print(f"Migration completed; manifest={manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
