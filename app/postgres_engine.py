"""Process-local PostgreSQL engine registry.

Task subprocesses cannot share live DBAPI connections with each other.  Inside a
process, however, every repository must use this registry so one bounded pool
owns the connection budget for a database/schema pair.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass

import psycopg2
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

DEFAULT_POSTGRES_POOL_SIZE = 4
MAX_POSTGRES_POOL_SIZE = 8
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class _EngineEntry:
    engine: Engine
    pool_size: int
    physical_connections_open: int = 0
    physical_connections_created: int = 0
    checkouts: int = 0
    queries: int = 0
    owners: int = 0


_lock = threading.RLock()
_engines: dict[tuple[str, str], _EngineEntry] = {}


def _normalize_url(database_url: str) -> str:
    value = str(database_url or "").strip()
    if not value:
        raise ValueError("database_url must be non-empty")
    if value.startswith("postgres://"):
        return "postgresql://" + value[len("postgres://") :]
    if value.startswith("postgresql+psycopg://"):
        return "postgresql+psycopg2://" + value.split("://", 1)[1]
    return value


def _normalize_schema(schema: str) -> str:
    value = str(schema or "monocorpus").strip() or "monocorpus"
    if not _SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Invalid database schema: {value!r}")
    return value


def _normalize_pool_size(pool_size: int) -> int:
    value = int(pool_size)
    if not 1 <= value <= MAX_POSTGRES_POOL_SIZE:
        raise ValueError(
            f"pool_size must be between 1 and {MAX_POSTGRES_POOL_SIZE}"
        )
    return value


def configured_postgres_pool_size() -> int:
    """Resolve the pool bound inherited by the current process."""
    raw = str(os.environ.get("MANZARA_DB_POOL_SIZE") or "").strip()
    return _normalize_pool_size(int(raw)) if raw else DEFAULT_POSTGRES_POOL_SIZE


def get_postgres_engine(
    database_url: str,
    *,
    schema: str = "monocorpus",
    pool_size: int | None = None,
) -> Engine:
    """Return the sole bounded engine for this process/database/schema."""
    normalized_url = _normalize_url(database_url)
    normalized_schema = _normalize_schema(schema)
    requested_size = None if pool_size is None else _normalize_pool_size(pool_size)
    key = (normalized_url, normalized_schema)
    with _lock:
        entry = _engines.get(key)
        if entry is not None:
            if requested_size is not None and entry.pool_size != requested_size:
                raise RuntimeError(
                    "PostgreSQL engine is already configured with "
                    f"pool_size={entry.pool_size} for schema={normalized_schema}; "
                    f"requested pool_size={requested_size}"
                )
            return entry.engine
        normalized_size = requested_size or configured_postgres_pool_size()
        engine = create_engine(
            normalized_url,
            pool_size=normalized_size,
            max_overflow=0,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_use_lifo=True,
            json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False),
            connect_args={
                "options": f"-csearch_path={normalized_schema},public",
                "connect_timeout": 10,
            },
        )
        entry = _EngineEntry(engine=engine, pool_size=normalized_size)
        if isinstance(engine, Engine):
            event.listen(engine, "connect", lambda *_args: _record_connect(entry))
            event.listen(engine, "close", lambda *_args: _record_close(entry))
            event.listen(engine, "checkout", lambda *_args: _record_checkout(entry))
        _engines[key] = entry
        return engine


def acquire_postgres_engine(
    database_url: str,
    *,
    schema: str = "monocorpus",
    pool_size: int | None = None,
) -> Engine:
    """Acquire an owned reference to a process-shared engine."""
    with _lock:
        engine = get_postgres_engine(
            database_url,
            schema=schema,
            pool_size=pool_size,
        )
        for entry in _engines.values():
            if entry.engine is engine:
                entry.owners += 1
                break
    return engine


def release_postgres_engine(engine: Engine) -> None:
    """Release an owner and dispose the pool after the final owner exits."""
    dispose = False
    with _lock:
        for key, entry in list(_engines.items()):
            if entry.engine is not engine:
                continue
            entry.owners = max(0, entry.owners - 1)
            if entry.owners == 0:
                del _engines[key]
                dispose = True
            break
    if dispose:
        engine.dispose()


def _record_connect(entry: _EngineEntry) -> None:
    with _lock:
        entry.physical_connections_open += 1
        entry.physical_connections_created += 1


def _record_close(entry: _EngineEntry) -> None:
    with _lock:
        entry.physical_connections_open = max(
            0, entry.physical_connections_open - 1
        )


def _record_checkout(entry: _EngineEntry) -> None:
    with _lock:
        entry.checkouts += 1


def record_postgres_query(engine: Engine) -> None:
    """Record one core-facade query against a registered engine."""
    with _lock:
        for entry in _engines.values():
            if entry.engine is engine:
                entry.queries += 1
                return


def get_postgres_engine_metrics(engine: Engine) -> dict[str, int]:
    """Return credential-free lifecycle counters for one shared engine."""
    with _lock:
        for entry in _engines.values():
            if entry.engine is engine:
                return {
                    "max_size": entry.pool_size,
                    "physical_connections_open": entry.physical_connections_open,
                    "physical_connections_created": entry.physical_connections_created,
                    "connections_in_use": int(engine.pool.checkedout()),
                    "idle_connections": max(
                        0,
                        entry.physical_connections_open
                        - int(engine.pool.checkedout()),
                    ),
                    "checkouts": entry.checkouts,
                    "queries": entry.queries,
                }
    raise RuntimeError("PostgreSQL engine is not registered")


def is_transient_postgres_error(exc: BaseException) -> bool:
    """Return whether an exception represents unavailable DB connectivity."""
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (psycopg2.OperationalError, psycopg2.InterfaceError)):
            return True
        if isinstance(current, DBAPIError) and bool(current.connection_invalidated):
            return True
        current = current.__cause__ or current.__context__
    return False


def dispose_all_postgres_engines() -> None:
    """Dispose every process-local pool, normally during process shutdown."""
    with _lock:
        entries = list(_engines.values())
        _engines.clear()
    for entry in entries:
        entry.engine.dispose()


__all__ = [
    "DEFAULT_POSTGRES_POOL_SIZE",
    "MAX_POSTGRES_POOL_SIZE",
    "acquire_postgres_engine",
    "configured_postgres_pool_size",
    "dispose_all_postgres_engines",
    "get_postgres_engine",
    "get_postgres_engine_metrics",
    "is_transient_postgres_error",
    "record_postgres_query",
    "release_postgres_engine",
]
