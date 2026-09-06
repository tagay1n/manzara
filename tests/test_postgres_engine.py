"""Shared PostgreSQL engine registry contracts."""

from __future__ import annotations

from unittest.mock import Mock

from sqlalchemy import text


def test_engine_is_shared_by_database_url_and_schema(monkeypatch) -> None:
    from app import postgres_engine

    created = []

    def create_engine(database_url, **kwargs):
        engine = Mock()
        engine.url = database_url
        created.append((engine, kwargs))
        return engine

    monkeypatch.setattr(postgres_engine, "create_engine", create_engine)
    postgres_engine.dispose_all_postgres_engines()

    first = postgres_engine.get_postgres_engine(
        "postgresql://example.test/manzara", schema="monocorpus", pool_size=2
    )
    second = postgres_engine.get_postgres_engine(
        "postgresql://example.test/manzara", schema="monocorpus"
    )

    assert first is second
    assert len(created) == 1
    assert created[0][1]["pool_size"] == 2
    assert created[0][1]["max_overflow"] == 0
    assert created[0][1]["pool_pre_ping"] is True


def test_different_schema_gets_an_isolated_engine(monkeypatch) -> None:
    from app import postgres_engine

    created = []

    def create_engine(_database_url, **_kwargs):
        engine = Mock()
        created.append(engine)
        return engine

    monkeypatch.setattr(postgres_engine, "create_engine", create_engine)
    postgres_engine.dispose_all_postgres_engines()

    first = postgres_engine.get_postgres_engine(
        "postgresql://example.test/manzara", schema="one", pool_size=1
    )
    second = postgres_engine.get_postgres_engine(
        "postgresql://example.test/manzara", schema="two", pool_size=1
    )

    assert first is not second
    assert len(created) == 2


def test_default_pool_size_comes_from_process_environment(monkeypatch) -> None:
    from app import postgres_engine

    created = []

    def create_engine(_database_url, **kwargs):
        engine = Mock()
        created.append((engine, kwargs))
        return engine

    monkeypatch.setenv("MANZARA_DB_POOL_SIZE", "1")
    monkeypatch.setattr(postgres_engine, "create_engine", create_engine)
    postgres_engine.dispose_all_postgres_engines()

    postgres_engine.get_postgres_engine(
        "postgresql://example.test/manzara", schema="monocorpus"
    )

    assert created[0][1]["pool_size"] == 1


def test_pool_size_conflict_is_rejected(monkeypatch) -> None:
    from app import postgres_engine

    monkeypatch.setattr(postgres_engine, "create_engine", lambda *_args, **_kwargs: Mock())
    postgres_engine.dispose_all_postgres_engines()
    postgres_engine.get_postgres_engine(
        "postgresql://example.test/manzara", schema="monocorpus", pool_size=1
    )

    try:
        postgres_engine.get_postgres_engine(
            "postgresql://example.test/manzara", schema="monocorpus", pool_size=2
        )
    except RuntimeError as exc:
        assert "already configured with pool_size=1" in str(exc)
    else:  # pragma: no cover - explicit assertion for a useful failure message.
        raise AssertionError("conflicting pool sizes must not create a second pool")


def test_core_and_task_repository_share_one_container_pool(
    prepared_test_schema: tuple[str, str],
) -> None:
    from app.db import Database
    from app.modules.library.document_cleanup_repository import (
        DocumentCleanupRepository,
    )
    from app.postgres_engine import get_postgres_engine_metrics

    database_url, schema = prepared_test_schema
    db = Database(database_url, schema=schema)
    repository = DocumentCleanupRepository(database_url, schema=schema)
    try:
        assert db._engine is repository.engine
        with repository.engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
        assert db.get_latest_event_id() >= 0

        metrics = get_postgres_engine_metrics(repository.engine)
        assert metrics["physical_connections_open"] <= 4
        assert metrics["max_size"] == 4
    finally:
        repository.dispose()
        db.close()
