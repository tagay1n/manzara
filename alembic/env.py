from __future__ import annotations

import os
import re
from logging.config import fileConfig
from pathlib import Path

import yaml
from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import DDL, create_engine, pool, text


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None
VERSION_TABLE = "alembic_version_manzara"


def _resolve_schema() -> str:
    config_schema = str(config.get_main_option("manzara_db_schema") or "").strip()
    if config_schema:
        return _validate_schema(config_schema)
    value = str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip()
    return _validate_schema(value or "monocorpus")


def _validate_schema(value: str) -> str:
    if len(value) > 63 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Invalid Alembic schema identifier")
    return value


def _resolve_version_schema() -> str:
    config_schema = str(config.get_main_option("manzara_alembic_version_schema") or "").strip()
    if config_schema:
        return _validate_schema(config_schema)
    value = str(os.environ.get("MANZARA_ALEMBIC_VERSION_SCHEMA") or "").strip()
    if value:
        return _validate_schema(value)
    return _resolve_schema()


def _candidate_config_paths() -> tuple[Path, ...]:
    repo_root = Path(__file__).resolve().parents[1]
    return (
        repo_root / "config.local.yaml",
        repo_root / "config.yaml",
    )


def _resolve_database_url() -> str:
    cfg_url = str(config.get_main_option("manzara_database_url") or "").strip()
    if cfg_url:
        return cfg_url

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

    raise RuntimeError(
        "Cannot resolve database URL for Alembic. Set MANZARA_DATABASE_URL or MANZARA_CONFIG_PATH."
    )


def run_migrations_offline() -> None:
    url = _resolve_database_url()
    version_schema = _resolve_version_schema()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        version_table=VERSION_TABLE,
        version_table_schema=version_schema,
        catalog_schema=_resolve_schema(),
    )

    with context.begin_transaction():
        _create_schemas()
        context.run_migrations()


def _create_schemas() -> None:
    existing = set()
    if not context.is_offline_mode():
        existing = set(context.get_context().bind.execute(text("SELECT nspname FROM pg_namespace")).scalars())
    for schema in dict.fromkeys((_resolve_schema(), _resolve_version_schema(), "public")):
        if schema not in existing:
            context.execute(DDL(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))


def _run_online(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table=VERSION_TABLE,
        version_table_schema=_resolve_version_schema(),
        catalog_schema=_resolve_schema(),
    )
    with context.begin_transaction():
        migration = context.get_context()
        known = {item.revision for item in ScriptDirectory.from_config(config).walk_revisions()}
        if set(migration.get_current_heads()) - known:
            raise RuntimeError(
                "Database revision is outside the retained Alembic history. "
                "For revisions below 20261008_0062, upgrade using commit c9782d1 first; "
                "never stamp an older schema. See docs/operations.md."
            )
        _create_schemas()
        context.run_migrations()


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        _run_online(supplied)
        return
    connectable = create_engine(_resolve_database_url(), poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            _run_online(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
