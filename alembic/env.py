from __future__ import annotations

from logging.config import fileConfig

from alembic.script import ScriptDirectory
from sqlalchemy import DDL, create_engine, pool, text

from alembic import context
from app.settings import configured_schema, load_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None
VERSION_TABLE = "alembic_version_manzara"


def _resolve_schema() -> str:
    return configured_schema("database_schema")


def _resolve_version_schema() -> str:
    return configured_schema("migration_version_schema")


def _resolve_database_url() -> str:
    return load_settings().database_url


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
