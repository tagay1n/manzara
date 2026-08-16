"""Metadata evaluation checkpoint migration tests."""

from sqlalchemy import create_engine, inspect


def test_metadata_evaluation_state_table_exists(
    prepared_test_schema,  # noqa: ANN001
) -> None:
    database_url, schema = prepared_test_schema
    engine = create_engine(database_url)
    inspector = inspect(engine)

    try:
        assert inspector.has_table(
            "library_metadata_evaluation_state",
            schema=schema,
        )
        columns = {
            item["name"]
            for item in inspector.get_columns(
                "library_metadata_evaluation_state",
                schema=schema,
            )
        }
        assert {
            "md5",
            "status",
            "attempts_json",
            "model_pool_json",
            "last_run_id",
            "terminal_reason",
        } <= columns
    finally:
        engine.dispose()
