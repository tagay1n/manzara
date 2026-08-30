"""PostgreSQL ownership coverage for document-dependent rows."""

from __future__ import annotations

from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_document_dependents_are_cleaned_and_cascade(
    prepared_test_schema: tuple[str, str],
) -> None:
    database_url, _prepared_schema = prepared_test_schema
    schema = f"manzara_document_cascade_{uuid.uuid4().hex[:10]}"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("manzara_database_url", database_url)
    config.set_main_option("manzara_db_schema", schema)
    config.set_main_option("manzara_alembic_version_schema", schema)
    engine = create_engine(database_url)
    existing_md5 = "a" * 32
    orphan_md5 = "b" * 32
    try:
        command.upgrade(config, "20260830_0041")
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'''CREATE TABLE "{schema}".document (
                        md5 TEXT,
                        document_url TEXT
                    )'''
                )
            )
            conn.execute(
                text(f'CREATE TABLE "{schema}".metadata (md5 TEXT NOT NULL)')
            )
            conn.execute(
                text(f'CREATE TABLE "{schema}".isbn_keep_many (md5 TEXT NOT NULL)')
            )
            conn.execute(
                text(f'INSERT INTO "{schema}".document (md5) VALUES (:md5)'),
                {"md5": existing_md5},
            )
            conn.execute(
                text(
                    f'''INSERT INTO "{schema}".library_upstream_metadata (
                        md5, payload_json, source_key, source_etag,
                        source_size, payload_sha256
                    ) VALUES
                        (:existing_md5, '{{}}'::jsonb, :existing_key, 'etag', 1, :sha),
                        (:orphan_md5, '{{}}'::jsonb, :orphan_key, 'etag', 1, :sha)'''
                ),
                {
                    "existing_md5": existing_md5,
                    "existing_key": f"{existing_md5}.zip",
                    "orphan_md5": orphan_md5,
                    "orphan_key": f"{orphan_md5}.zip",
                    "sha": "c" * 64,
                },
            )
            conn.execute(
                text(f'INSERT INTO "{schema}".metadata (md5) VALUES (:md5)'),
                {"md5": existing_md5},
            )
            conn.execute(
                text(f'INSERT INTO "{schema}".isbn_keep_many (md5) VALUES (:md5)'),
                {"md5": existing_md5},
            )

        command.upgrade(config, "head")

        inspector = inspect(engine)
        unique_columns = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints("document", schema=schema)
        }
        assert ("md5",) in unique_columns
        for table_name in (
            "library_upstream_metadata",
            "library_metadata_evaluation_state",
            "library_metadata_extraction_state",
            "library_metadata_quality_state",
            "library_non_pdf_extraction_state",
            "library_book_previews",
            "library_collection_document_features",
            "library_collection_items",
            "library_collection_proposal_items",
            "metadata",
            "isbn_keep_many",
        ):
            foreign_keys = inspector.get_foreign_keys(table_name, schema=schema)
            assert any(
                item["constrained_columns"] == ["md5"]
                and item["referred_table"] == "document"
                and item["options"].get("ondelete") == "CASCADE"
                for item in foreign_keys
            ), table_name

        with engine.begin() as conn:
            remaining = conn.execute(
                text(
                    f'SELECT md5 FROM "{schema}".library_upstream_metadata '
                    "ORDER BY md5"
                )
            ).scalars().all()
            assert remaining == [existing_md5]
            conn.execute(
                text(f'DELETE FROM "{schema}".document WHERE md5=:md5'),
                {"md5": existing_md5},
            )
            assert conn.execute(
                text(
                    f'SELECT count(*) FROM "{schema}".library_upstream_metadata'
                )
            ).scalar_one() == 0
            assert conn.execute(
                text(f'SELECT count(*) FROM "{schema}".metadata')
            ).scalar_one() == 0
            assert conn.execute(
                text(f'SELECT count(*) FROM "{schema}".isbn_keep_many')
            ).scalar_one() == 0
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
