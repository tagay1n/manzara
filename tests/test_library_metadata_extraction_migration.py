"""Alembic coverage for resumable Library metadata extraction state."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app.modules.library.metadata_extraction import MetadataExtractionRepository


def test_metadata_extraction_state_table_is_migrated(prepared_test_schema) -> None:
    database_url, schema = prepared_test_schema
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert inspector.has_table("library_metadata_extraction_state", schema=schema)
        columns = {
            item["name"]
            for item in inspector.get_columns(
                "library_metadata_extraction_state", schema=schema
            )
        }
        assert {
            "md5",
            "status",
            "attempts_json",
            "model_pool_json",
            "last_run_id",
            "terminal_reason",
            "prompt_version",
            "prompt_version",
            "retry_after",
            "operational_failure_count",
            "last_operational_error",
            "created_at",
            "updated_at",
        }.issubset(columns)
        assert inspector.has_table("library_metadata_quality_state", schema=schema)
        quality_columns = {
            item["name"]
            for item in inspector.get_columns(
                "library_metadata_quality_state", schema=schema
            )
        }
        assert {
            "md5",
            "contract_version",
            "status",
            "issues_json",
            "last_run_id",
            "detected_at",
            "resolved_at",
            "updated_at",
        }.issubset(quality_columns)
    finally:
        engine.dispose()


def test_metadata_success_is_transactional_against_json_column(
    prepared_test_schema,
) -> None:
    database_url, schema = prepared_test_schema
    engine = create_engine(database_url)
    repository = None
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'DROP TABLE IF EXISTS "{schema}".library_metadata_quality_state'
                )
            )
            conn.execute(text(f'DROP TABLE IF EXISTS "{schema}".metadata'))
            conn.execute(text(f'DROP TABLE IF EXISTS "{schema}".document'))
            conn.execute(
                text(
                    f"""
                    CREATE TABLE "{schema}".document (
                        md5 TEXT,
                        language TEXT,
                        meta_extraction_method TEXT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    f"""
                    CREATE TABLE "{schema}".metadata (
                        md5 TEXT PRIMARY KEY,
                        schema_org JSON,
                        lib BOOLEAN,
                        lib_eval_method TEXT,
                        classification_id BIGINT
                    )
                    """
                )
            )
            conn.execute(
                text(
                    f"""
                    CREATE TABLE "{schema}".library_metadata_quality_state (
                        md5 TEXT PRIMARY KEY REFERENCES "{schema}".metadata(md5),
                        contract_version TEXT NOT NULL,
                        status TEXT NOT NULL,
                        issues_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                        resolved_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ
                    )
                    """
                )
            )
            conn.execute(
                text(f'INSERT INTO "{schema}".document (md5) VALUES (:md5)'),
                {"md5": "a" * 32},
            )

        repository = MetadataExtractionRepository(database_url, schema=schema)
        assert repository.save_success(
            "a" * 32,
            schema_org={
                "@context": "https://schema.org",
                "@type": "Book",
                "name": "Kitap",
                "inLanguage": "tt-Cyrl",
            },
            model_name="model-one",
        )

        with engine.connect() as conn:
            metadata = conn.execute(
                text(f'SELECT schema_org FROM "{schema}".metadata')
            ).scalar_one()
            document = conn.execute(
                text(
                    f'SELECT language, meta_extraction_method FROM "{schema}".document'
                )
            ).mappings().one()
        assert metadata["name"] == "Kitap"
        assert document["language"] == "tt-Cyrl"
        assert document["meta_extraction_method"] == "model-one/prompt.v4"
    finally:
        if repository is not None:
            repository.dispose()
        with engine.begin() as conn:
            conn.execute(
                text(
                    f'DROP TABLE IF EXISTS "{schema}".library_metadata_quality_state'
                )
            )
            conn.execute(text(f'DROP TABLE IF EXISTS "{schema}".metadata'))
            conn.execute(text(f'DROP TABLE IF EXISTS "{schema}".document'))
        engine.dispose()
