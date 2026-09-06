"""Focused tests for the bounded PostgreSQL connection lifecycle."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.bootstrap import shutdown_app
from app.repositories.core import CoreRepository, _SharedEnginePool


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self.connection = connection

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: object, params: object = None) -> None:
        self.connection.statements.append((str(query), params))

    def close(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, number: int) -> None:
        self.number = number
        self.closed = 0
        self.commits = 0
        self.rollbacks = 0
        self.statements: list[tuple[str, object]] = []

    def cursor(self, **_kwargs: object) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = 1

    def get_transaction_status(self) -> int:
        return 0


class _ConnectionFactory:
    def __init__(self) -> None:
        self.connections: list[_FakeConnection] = []
        self._lock = threading.Lock()

    def __call__(self, _database_url: str) -> _FakeConnection:
        with self._lock:
            connection = _FakeConnection(len(self.connections) + 1)
            self.connections.append(connection)
            return connection


def test_shared_engine_pool_recognizes_healthy_and_broken_connections() -> None:
    pool = _SharedEnginePool(engine=object())
    healthy = _FakeConnection(1)
    broken = _FakeConnection(2)
    broken.closed = 1

    assert pool._is_broken(healthy) is False
    assert pool._is_broken(broken) is True


def _repository(factory: _ConnectionFactory, *, pool_size: int = 4) -> CoreRepository:
    return CoreRepository(
        "postgresql://example.invalid/manzara",
        schema="isolated_test",
        pool_size=pool_size,
        connection_factory=factory,
    )


def test_connection_is_reused_and_search_path_is_configured_once() -> None:
    factory = _ConnectionFactory()
    repository = _repository(factory)

    with repository._connect():
        pass
    with repository._connect():
        pass

    assert len(factory.connections) == 1
    setup_statements = [query for query, _params in factory.connections[0].statements]
    assert len(setup_statements) == 1
    assert "SET search_path" in setup_statements[0]
    assert all("CREATE SCHEMA" not in query for query in setup_statements)


@pytest.mark.parametrize(
    ("database_url", "expected_url"),
    [
        ("postgres://host/db", "postgres://host/db"),
        ("postgresql://host/db", "postgresql://host/db"),
        ("postgresql+psycopg2://host/db", "postgresql://host/db"),
    ],
)
def test_supported_postgres_urls_reach_connection_factory(
    database_url: str, expected_url: str
) -> None:
    received: list[str] = []
    factory = _ConnectionFactory()

    def capture(url: str) -> _FakeConnection:
        received.append(url)
        return factory(url)

    repository = CoreRepository(database_url, connection_factory=capture)
    with repository._connect():
        pass

    assert received == [expected_url]


def test_exception_rolls_back_before_connection_is_reused() -> None:
    factory = _ConnectionFactory()
    repository = _repository(factory)

    with pytest.raises(RuntimeError, match="boom"):
        with repository._connect():
            raise RuntimeError("boom")

    connection = factory.connections[0]
    assert connection.rollbacks == 1
    with repository._connect() as adapter:
        assert adapter._conn is connection


def test_broken_connection_is_discarded_and_replaced() -> None:
    factory = _ConnectionFactory()
    repository = _repository(factory)

    with repository._connect() as adapter:
        first = adapter._conn
        first.closed = 1

    with repository._connect() as adapter:
        second = adapter._conn

    assert second is not first
    assert len(factory.connections) == 2


def test_pool_size_is_bounded_and_thread_safe() -> None:
    factory = _ConnectionFactory()
    repository = _repository(factory, pool_size=2)
    active_ids: set[int] = set()
    maximum_active = 0
    lock = threading.Lock()

    def use_connection() -> None:
        nonlocal maximum_active
        with repository._connect() as adapter:
            connection_id = id(adapter._conn)
            with lock:
                assert connection_id not in active_ids
                active_ids.add(connection_id)
                maximum_active = max(maximum_active, len(active_ids))
            time.sleep(0.02)
            with lock:
                active_ids.remove(connection_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: use_connection(), range(12)))

    assert maximum_active == 2
    assert len(factory.connections) == 2


def test_close_closes_idle_connections_and_rejects_new_checkouts() -> None:
    factory = _ConnectionFactory()
    repository = _repository(factory)
    with repository._connect():
        pass

    repository.close()

    assert factory.connections[0].closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        with repository._connect():
            pass


def test_application_shutdown_closes_database_pool() -> None:
    calls: list[str] = []

    class _Database:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True
            calls.append("database.close")

    class _Runner:
        def shutdown(self) -> None:
            calls.append("runner.shutdown")

    state = type(
        "State",
        (),
        {"shutting_down": False, "db": _Database(), "runner": _Runner()},
    )()

    shutdown_app(state=state)

    assert state.shutting_down is True
    assert state.db.closed is True
    assert calls == ["runner.shutdown", "database.close"]
