"""Cross-task PostgreSQL advisory lock for document synchronization."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text


@contextmanager
def document_sync_lock(database_url: str, *, schema: str) -> Iterator[None]:
    """Prevent catalog migration and cleanup synchronization from overlapping."""
    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema},public"},
    )
    connection = engine.connect()
    acquired = bool(
        connection.execute(
            text("SELECT pg_try_advisory_lock(hashtext('manzara.document_sync'))")
        ).scalar_one()
    )
    if not acquired:
        connection.close()
        engine.dispose()
        raise RuntimeError("Another document synchronization task is already running")
    try:
        yield
    finally:
        connection.execute(
            text("SELECT pg_advisory_unlock(hashtext('manzara.document_sync'))")
        )
        connection.close()
        engine.dispose()


__all__ = ["document_sync_lock"]
