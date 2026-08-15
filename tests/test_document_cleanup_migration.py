"""Alembic coverage for guarded document cleanup persistence."""

from __future__ import annotations

from sqlalchemy import inspect, text


def test_document_cleanup_tables_are_migrated(prepared_test_schema) -> None:
    database_url, schema = prepared_test_schema
    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert inspector.has_table("document_cleanup_queue", schema=schema)
        assert inspector.has_table("library_isbn_duplicate_reviews", schema=schema)
        queue_columns = {
            item["name"]
            for item in inspector.get_columns("document_cleanup_queue", schema=schema)
        }
        assert {"cleanup_id", "phase", "evidence_json", "last_error"}.issubset(
            queue_columns
        )
        with engine.begin() as conn:
            for status in ("canceled", "recovered"):
                conn.execute(
                    text(
                        f'''INSERT INTO "{schema}".document_cleanup_queue (
                            scope, action, reason, md5, source_path, status
                        ) VALUES (
                            'document', 'move', 'test', :md5, '/test', :status
                        )'''
                    ),
                    {"md5": status, "status": status},
                )
    finally:
        engine.dispose()
