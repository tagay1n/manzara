"""Maintenance composition for catalog-native primary document transfer."""

from app.catalog.document_transfer import DocumentTransferStore
from app.postgres_engine import acquire_postgres_engine, release_postgres_engine


class PostgresDocumentSyncRepository(DocumentTransferStore):
    def __init__(self, database_url, *, schema):
        engine = acquire_postgres_engine(database_url, schema=schema)
        super().__init__(engine, schema=schema)

    def dispose(self):
        release_postgres_engine(self.engine)
