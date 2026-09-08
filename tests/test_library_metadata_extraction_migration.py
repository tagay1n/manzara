"""Alembic coverage for resumable Library metadata extraction state."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app.local_state import AIItemCheckpointStore, LocalStateStore
from app.modules.library.metadata_extraction import MetadataExtractionRepository


def test_disposable_runtime_state_is_removed_from_cloud(prepared_test_schema) -> None:
    database_url, schema = prepared_test_schema
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        for table_name in (
            "panel_definitions",
            "task_definitions",
            "runs",
            "events",
            "run_logs",
            "conveyor_definitions",
            "conveyor_runs",
            "conveyor_run_items",
            "gemini_keys",
            "gemini_key_model_state",
            "gemini_runtime_control",
            "gemini_account_leases",
            "gemini_model_runtime",
            "library_metadata_extraction_state",
            "library_metadata_evaluation_state",
        ):
            assert not inspector.has_table(table_name, schema=schema), table_name
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
    tmp_path,
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
                            detected_at TIMESTAMPTZ,
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

        local_path = tmp_path / "runtime.sqlite3"
        LocalStateStore(local_path).initialize()
        repository = MetadataExtractionRepository(
            database_url,
            schema=schema,
            checkpoint_store=AIItemCheckpointStore(local_path),
        )
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
        assert document["meta_extraction_method"] == "model-one/prompt.v7"
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
