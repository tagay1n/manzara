"""Catalog publisher analysis composition and session locking."""

from __future__ import annotations


class PublisherMergeRepository:
    def publisher_catalog(self):
        """Compose catalog-native generation without the retired review workbench."""
        from app.catalog.publisher_analysis import PublisherCatalogStore
        from app.catalog.repository import CatalogRepository

        if self._engine is None:
            raise RuntimeError("Publisher clustering requires the shared PostgreSQL engine")
        return PublisherCatalogStore(CatalogRepository(self._engine, schema=self.schema))


    def publisher_analysis_lock(self):
        """Reserve one session, leaving another for short catalog transactions."""
        if self.pool_size < 2:
            raise ValueError(
                "Publisher analysis requires a database pool of at least two connections."
            )
        from contextlib import contextmanager

        @contextmanager
        def locked():
            with self._connect() as conn:
                acquired = conn.execute(
                    "SELECT pg_try_advisory_lock(726193450) AS acquired"
                ).fetchone()["acquired"]
                if not acquired:
                    raise ValueError("A publisher analysis is already active.")
                # Session advisory locks survive COMMIT. Avoid an idle transaction
                # throughout the potentially hour-long Codex analysis.
                conn.execute("COMMIT")
                try:
                    yield
                finally:
                    conn.execute("SELECT pg_advisory_unlock(726193450)")

        return locked()
