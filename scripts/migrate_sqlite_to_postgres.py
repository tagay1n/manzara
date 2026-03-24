#!/usr/bin/env python3
"""One-time migration of Manzara operational data from SQLite to PostgreSQL."""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import yaml
from sqlalchemy import create_engine, text


TABLE_ORDER = (
    "task_definitions",
    "panel_definitions",
    "runs",
    "run_logs",
    "events",
    "workflows",
    "workflow_steps",
    "workflow_schedules",
    "workflow_runs",
    "workflow_step_runs",
    "normalization_canonicals",
    "normalization_aliases",
    "normalization_suggestions",
    "normalization_events",
)

SERIAL_COLUMNS = {
    "runs": "run_id",
    "run_logs": "log_id",
    "events": "event_id",
    "workflow_runs": "workflow_run_id",
    "workflow_step_runs": "step_run_id",
    "normalization_canonicals": "canonical_id",
    "normalization_aliases": "alias_id",
    "normalization_suggestions": "suggestion_id",
    "normalization_events": "event_id",
}


def _candidate_config_paths() -> Iterable[Path]:
    root = Path(__file__).resolve().parents[1]
    return (
        root / "config.local.yaml",
        root / "config.yaml",
        root / "config.example.yaml",
    )


def _load_database_url() -> str:
    env_url = str(os.environ.get("MANZARA_DATABASE_URL") or "").strip()
    if env_url:
        return env_url

    env_cfg = str(os.environ.get("MANZARA_CONFIG_PATH") or "").strip()
    if env_cfg:
        cfg_path = Path(env_cfg).expanduser()
        payload = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        db_url = str(payload.get("database_url") or "").strip()
        if db_url:
            return db_url

    for path in _candidate_config_paths():
        if not path.exists():
            continue
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        db_url = str(payload.get("database_url") or "").strip()
        if db_url and "<REDACTED>" not in db_url:
            return db_url

    raise ValueError("Cannot resolve PostgreSQL database_url from env/config")


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row[1]) for row in rows]


def _sqlite_row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _sqlite_fetch_rows(conn: sqlite3.Connection, table: str, columns: list[str]) -> list[dict[str, Any]]:
    col_expr = ", ".join(columns)
    cur = conn.execute(f"SELECT {col_expr} FROM {table}")
    rows = cur.fetchall()
    payload: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for index, column in enumerate(columns):
            item[column] = row[index]
        payload.append(item)
    return payload


def _pick_first_value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value) != "":
            return value
    return None


def _pg_table_exists(conn, schema: str, table: str) -> bool:
    return bool(
        conn.execute(
            text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = :schema AND table_name = :table
                )
                """
            ),
            {"schema": schema, "table": table},
        ).scalar()
    )


def _pg_columns(conn, schema: str, table: str) -> list[str]:
    rows = conn.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = :schema
              AND table_name = :table
            ORDER BY ordinal_position
            """
        ),
        {"schema": schema, "table": table},
    ).scalars().all()
    return [str(item) for item in rows]


def _truncate_table(conn, schema: str, table: str) -> None:
    conn.execute(text(f'TRUNCATE TABLE "{schema}"."{table}" RESTART IDENTITY CASCADE'))


def _copy_table(sqlite_conn: sqlite3.Connection, pg_conn, schema: str, table: str) -> tuple[int, int, list[str]]:
    columns = _sqlite_columns(sqlite_conn, table)
    if not columns:
        return 0, 0, []
    target_columns = _pg_columns(pg_conn, schema, table)
    common_columns = [column for column in columns if column in set(target_columns)]
    if not common_columns:
        return 0, _sqlite_row_count(sqlite_conn, table), []
    rows = _sqlite_fetch_rows(sqlite_conn, table, common_columns)
    if not rows:
        return 0, _sqlite_row_count(sqlite_conn, table), common_columns

    insert_columns = list(common_columns)

    need_created = "created_at" in target_columns and "created_at" not in common_columns
    need_updated = "updated_at" in target_columns and "updated_at" not in common_columns

    if need_created:
        insert_columns.append("created_at")
    if need_updated:
        insert_columns.append("updated_at")

    if need_created or need_updated:
        for row in rows:
            inferred_created = _pick_first_value(
                row,
                ("created_at", "started_at", "ts", "finished_at", "heartbeat_at"),
            )
            if inferred_created is None:
                inferred_created = "1970-01-01T00:00:00+00:00"

            inferred_updated = _pick_first_value(
                row,
                ("updated_at", "heartbeat_at", "finished_at", "started_at", "ts"),
            )
            if inferred_updated is None:
                inferred_updated = inferred_created

            if need_created:
                row["created_at"] = inferred_created
            if need_updated:
                row["updated_at"] = inferred_updated

    col_sql = ", ".join([f'"{col}"' for col in insert_columns])
    bind_sql = ", ".join([f":{col}" for col in insert_columns])
    insert_sql = text(f'INSERT INTO "{schema}"."{table}" ({col_sql}) VALUES ({bind_sql})')
    pg_conn.execute(insert_sql, rows)
    return len(rows), _sqlite_row_count(sqlite_conn, table), insert_columns


def _resequence(conn, schema: str, table: str, id_column: str) -> None:
    seq_name = conn.execute(
        text("SELECT pg_get_serial_sequence(:qualified_table, :id_column)"),
        {"qualified_table": f"{schema}.{table}", "id_column": id_column},
    ).scalar()
    if not seq_name:
        return
    max_id = conn.execute(
        text(f'SELECT COALESCE(MAX("{id_column}"), 0) FROM "{schema}"."{table}"')
    ).scalar()
    max_id_int = int(max_id or 0)
    if max_id_int <= 0:
        conn.execute(text("SELECT setval(:seq_name, 1, false)"), {"seq_name": seq_name})
        return
    conn.execute(text("SELECT setval(:seq_name, :value, true)"), {"seq_name": seq_name, "value": max_id_int})


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite operational DB into PostgreSQL schema")
    parser.add_argument(
        "--sqlite-path",
        default="data/manzara.db",
        help="Path to SQLite database file",
    )
    parser.add_argument(
        "--schema",
        default="monocorpus",
        help="Target PostgreSQL schema",
    )
    parser.add_argument(
        "--truncate-first",
        action="store_true",
        help="Truncate target tables before copying rows",
    )
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite_path)
    if not sqlite_path.exists():
        raise FileNotFoundError(f"SQLite file does not exist: {sqlite_path}")

    db_url = _load_database_url()
    sqlite_conn = sqlite3.connect(str(sqlite_path))
    try:
        pg_engine = create_engine(db_url)
        try:
            with pg_engine.begin() as pg_conn:
                pg_conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{args.schema}"'))

                existing_tables: list[str] = []
                for table in TABLE_ORDER:
                    if _pg_table_exists(pg_conn, args.schema, table):
                        existing_tables.append(table)
                    else:
                        print(f"SKIP_NO_TARGET_TABLE {table}")

                if args.truncate_first:
                    for table in reversed(existing_tables):
                        _truncate_table(pg_conn, args.schema, table)
                        print(f"TRUNCATED {table}")

                for table in existing_tables:
                    sqlite_columns = _sqlite_columns(sqlite_conn, table)
                    inserted, sqlite_count, common_columns = _copy_table(
                        sqlite_conn,
                        pg_conn,
                        args.schema,
                        table,
                    )
                    skipped_columns = [col for col in sqlite_columns if col not in set(common_columns)]
                    print(f"COPIED {table} inserted={inserted} sqlite_rows={sqlite_count}")
                    if skipped_columns:
                        print(f"  SKIPPED_COLUMNS {table}: {', '.join(skipped_columns)}")

                for table, column in SERIAL_COLUMNS.items():
                    if table in existing_tables:
                        _resequence(pg_conn, args.schema, table, column)
                        print(f"RESEQUENCED {table}.{column}")
        finally:
            pg_engine.dispose()
    finally:
        sqlite_conn.close()


if __name__ == "__main__":
    main()
