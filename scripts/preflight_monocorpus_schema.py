#!/usr/bin/env python3
"""Preflight checks for Manzara PostgreSQL cutover into schema `monocorpus`."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml
from sqlalchemy import create_engine, text


MANZARA_TABLES = (
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


@dataclass(frozen=True)
class RuntimeConfig:
    database_url: str
    source: str


def _candidate_config_paths() -> Iterable[Path]:
    root = Path(__file__).resolve().parents[1]
    return (
        root / "config.local.yaml",
        root / "config.yaml",
        root / "config.example.yaml",
    )


def load_database_url() -> RuntimeConfig:
    env_url = str(os.environ.get("MANZARA_DATABASE_URL") or "").strip()
    if env_url:
        return RuntimeConfig(database_url=env_url, source="MANZARA_DATABASE_URL")

    env_cfg = str(os.environ.get("MANZARA_CONFIG_PATH") or "").strip()
    if env_cfg:
        cfg_path = Path(env_cfg).expanduser()
        if not cfg_path.exists():
            raise FileNotFoundError(f"MANZARA_CONFIG_PATH does not exist: {cfg_path}")
        payload = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        db_url = str(payload.get("database_url") or "").strip()
        if not db_url:
            raise ValueError(f"database_url is missing in {cfg_path}")
        return RuntimeConfig(database_url=db_url, source=str(cfg_path))

    for path in _candidate_config_paths():
        if not path.exists():
            continue
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        db_url = str(payload.get("database_url") or "").strip()
        if not db_url:
            continue
        if "<REDACTED>" in db_url:
            continue
        return RuntimeConfig(database_url=db_url, source=str(path))

    raise ValueError("Cannot find unmasked database_url in env or config files")


def main() -> None:
    parser = argparse.ArgumentParser(description="Preflight check for Postgres schema cutover")
    parser.add_argument("--schema", default="monocorpus", help="Target schema for Manzara tables")
    parser.add_argument(
        "--create-schema",
        action="store_true",
        help="Create target schema when missing",
    )
    args = parser.parse_args()

    cfg = load_database_url()
    engine = create_engine(cfg.database_url)
    try:
        with engine.begin() as conn:
            schema_exists = bool(
                conn.execute(
                    text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = :schema)"),
                    {"schema": args.schema},
                ).scalar()
            )
            if not schema_exists and args.create_schema:
                conn.execute(text(f'CREATE SCHEMA "{args.schema}"'))
                schema_exists = True

        with engine.connect() as conn:
            all_counts = conn.execute(
                text(
                    """
                    SELECT table_schema, COUNT(*) AS count
                    FROM information_schema.tables
                    WHERE table_type = 'BASE TABLE'
                    GROUP BY table_schema
                    ORDER BY count DESC, table_schema ASC
                    """
                )
            ).mappings().all()

            existing_target = conn.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = :schema
                    ORDER BY table_name
                    """
                ),
                {"schema": args.schema},
            ).scalars().all()

            public_tables = conn.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    ORDER BY table_name
                    """
                )
            ).scalars().all()

        existing_set = set(existing_target)
        collisions = [name for name in MANZARA_TABLES if name in existing_set]

        print(f"CONFIG_SOURCE={cfg.source}")
        print(f"TARGET_SCHEMA={args.schema}")
        print(f"SCHEMA_EXISTS={schema_exists}")
        print("SCHEMA_TABLE_COUNTS:")
        for row in all_counts:
            print(f"  {row['table_schema']}: {int(row['count'])}")
        print(f"TARGET_SCHEMA_TABLES={len(existing_target)}")
        print(f"MANZARA_NAME_COLLISIONS={len(collisions)}")
        for name in collisions:
            print(f"  COLLISION={name}")
        print(f"PUBLIC_TABLES={len(public_tables)}")
        if public_tables:
            print("PUBLIC_SAMPLE=" + ",".join(public_tables[:20]))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

