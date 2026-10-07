"""Alembic installs the catalog without changing existing durable entries."""

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.catalog.schema import build_metadata


def test_catalog_upgrade_repairs_early_proposal_members_without_row_identity(test_database_url):
    schema = "catalog_early_" + uuid.uuid4().hex[:10]
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    for key, value in {"manzara_database_url": test_database_url,
                       "manzara_db_schema": schema,
                       "manzara_alembic_version_schema": schema}.items():
        config.set_main_option(key, value)
    engine = create_engine(test_database_url)
    try:
        command.upgrade(config, "20261005_0056")
        with engine.begin() as conn:
            for table in ('entity_roles','identifiers','genres','subjects','audiences','sufficient_modes','references','reference_authors','preview_pages'):
                conn.execute(text(f'ALTER TABLE "{schema}".catalog_{table} DROP CONSTRAINT catalog_{table}_pkey'))
            # The first installed 0056 schema had no proposal-member row ID.
            conn.execute(text(f'ALTER TABLE "{schema}".catalog_proposal_members DROP COLUMN member_id'))
            for table, columns in {"publications": ("name", "description", "edition"),
                                   "entities": ("display_name",), "names": ("raw_name",),
                                   "collections": ("title",), "classification_nodes": ("label_en", "label_tt")}.items():
                for column in columns:
                    conn.execute(text(f'DROP INDEX "{schema}"."idx_catalog_{table}_{column}_trgm"'))
            name = conn.execute(text(f'''INSERT INTO "{schema}".catalog_names(kind,raw_name)
                VALUES ('organization','Preserved') RETURNING name_id''')).scalar_one()
            proposal = conn.execute(text(f'''INSERT INTO "{schema}".catalog_proposals(kind)
                VALUES ('identity') RETURNING proposal_id''')).scalar_one()
            conn.execute(text(f'''INSERT INTO "{schema}".catalog_proposal_members
                (proposal_id,name_id,snapshot) VALUES (:proposal,:name,'{{}}')'''),
                {"proposal": proposal, "name": name})
        command.upgrade(config, "head")
        with engine.connect() as conn:
            row = conn.execute(text(f'SELECT * FROM "{schema}".catalog_proposal_members')).mappings().one()
            assert row["member_id"] > 0
            assert row["proposal_id"] == proposal
            assert row["name_id"] == name
        assert inspect(engine).get_pk_constraint("catalog_proposal_members", schema=schema)["constrained_columns"] == ["member_id"]
        for table in build_metadata(schema).tables.values():
            assert inspect(engine).get_pk_constraint(table.name, schema=schema)['constrained_columns'] == list(table.primary_key.columns.keys())

        indexes = inspect(engine).get_indexes("catalog_names", schema=schema)
        assert any(index["name"] == "idx_catalog_names_raw_name_trgm" for index in indexes)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_catalog_upgrade_preserves_legacy_records_and_adds_search_indexes(test_database_url):
    schema = "catalog_migration_" + uuid.uuid4().hex[:10]
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", test_database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(test_database_url)
    try:
        command.upgrade(config, "20261001_0055")
        with engine.begin() as conn:
            conn.execute(text(f'''INSERT INTO "{schema}".normalization_canonicals
                (entity_type, display_name, normalized_name, status, created_at, updated_at)
                VALUES ('publisher', 'Preserved', 'preserved', 'active', 'now', 'now')'''))
        command.upgrade(config, "head")
        with engine.connect() as conn:
            assert conn.execute(text(f'SELECT display_name FROM "{schema}".normalization_canonicals')).scalar_one() == "Preserved"
            assert conn.execute(text(f'SELECT count(*) FROM "{schema}".catalog_documents')).scalar_one() == 0
            indexes = conn.execute(text("SELECT indexdef FROM pg_indexes WHERE schemaname=:schema"), {"schema": schema}).scalars().all()
            assert any("gin_trgm_ops" in index and "catalog_entities" in index for index in indexes)
        inspector = inspect(engine)
        for table in build_metadata(schema).tables.values():
            assert {column["name"] for column in inspector.get_columns(table.name, schema=schema)} == set(table.c.keys())
            assert inspector.get_pk_constraint(table.name, schema=schema)["constrained_columns"] == list(table.primary_key.columns.keys())
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
