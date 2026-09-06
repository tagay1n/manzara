"""Tests for the PostgreSQL Testcontainers lifecycle used by pytest."""

from __future__ import annotations

import conftest
import pytest
from sqlalchemy import create_engine, text


class _FakePostgresContainer:
    def __init__(
        self,
        image: str,
        *,
        driver: str,
        username: str = "manzara_test",
        password: str = "manzara_test",
        dbname: str = "manzara_test",
    ) -> None:
        self.image = image
        self.driver = driver
        self.username = username
        self.password = password
        self.dbname = dbname
        self.started = 0
        self.stopped = 0

    def start(self) -> _FakePostgresContainer:
        self.started += 1
        return self

    def stop(self) -> None:
        self.stopped += 1

    def get_connection_url(self) -> str:
        return "postgresql+psycopg2://test:test@127.0.0.1:54321/test"


def test_start_test_postgres_returns_container_owned_url() -> None:
    created: list[_FakePostgresContainer] = []

    def factory(
        image: str,
        *,
        driver: str,
        username: str,
        password: str,
        dbname: str,
    ) -> _FakePostgresContainer:
        container = _FakePostgresContainer(
            image,
            driver=driver,
            username=username,
            password=password,
            dbname=dbname,
        )
        created.append(container)
        return container

    container, database_url = conftest._start_test_postgres(factory)
    try:
        assert database_url == (
            "postgresql+psycopg2://test:test@127.0.0.1:54321/test"
        )
        assert container.image == conftest.TEST_POSTGRES_IMAGE
        assert container.driver == "psycopg2"
        assert container.username == "manzara_test"
        assert container.password == "manzara_test"
        assert container.dbname == "manzara_test"
        assert container.started == 1
    finally:
        container.stop()

    assert container.stopped == 1


def test_start_test_postgres_reports_container_startup_failure() -> None:
    class BrokenContainer(_FakePostgresContainer):
        def start(self) -> _FakePostgresContainer:
            raise OSError("docker socket unavailable")

    with pytest.raises(RuntimeError, match="Docker.*PostgreSQL-backed tests"):
        conftest._start_test_postgres(BrokenContainer)


def test_start_test_postgres_reports_container_construction_failure() -> None:
    def broken_factory(_image: str, **_kwargs: str) -> _FakePostgresContainer:
        raise OSError("docker socket unavailable")

    with pytest.raises(RuntimeError, match="Docker.*PostgreSQL-backed tests"):
        conftest._start_test_postgres(broken_factory)


def test_database_url_fixture_owns_session_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _FakePostgresContainer(
        conftest.TEST_POSTGRES_IMAGE,
        driver="psycopg2",
    )
    database_url = container.get_connection_url()
    monkeypatch.setattr(
        conftest,
        "_start_test_postgres",
        lambda: (container, database_url),
    )

    fixture = conftest.test_database_url
    assert fixture._fixture_function_marker.scope == "session"
    lifecycle = fixture.__wrapped__()
    assert next(lifecycle) == database_url
    with pytest.raises(StopIteration):
        next(lifecycle)

    assert container.stopped == 1


def test_testcontainer_provides_required_postgres_features(
    test_database_url: str,
) -> None:
    engine = create_engine(test_database_url)
    try:
        with engine.begin() as connection:
            version_num = int(
                connection.execute(
                    text("SELECT current_setting('server_version_num')")
                ).scalar_one()
            )
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            extension_version = connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'")
            ).scalar_one()
    finally:
        engine.dispose()

    assert version_num // 10_000 == 18
    assert extension_version
