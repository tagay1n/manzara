"""Shared transactional primitives for CLI and workflow catalog operations."""

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.catalog.contracts import (
    CatalogConflict,
    CatalogNotFound,
    integer,
    nonblank,
)
from app.catalog.metadata_store import MetadataStore
from app.catalog.personality_normalization import PersonalityNormalizationStore
from app.catalog.previews import PreviewStore
from app.catalog.schema import build_metadata
from app.document_operation_lock import lock_document_transaction

RECORDS = {
    "publication": ("publications", "publication_id"),
    "document": ("documents", "md5"), "entity": ("entities", "entity_id"),
    "collection": ("collections", "collection_id"),
    "classification_node": ("classification_nodes", "node_id"),
    "proposal": ("proposals", "proposal_id"),
    "alias": ("aliases", "alias_id"), "contribution": ("contributions", "contribution_id"),
}


def snapshot(value: Any) -> Any:
    """Serialize audit timestamps without leaking object representations."""
    return json.loads(json.dumps(value, default=lambda item: item.isoformat()))


class CatalogRepository(MetadataStore, PreviewStore, PersonalityNormalizationStore):
    def __init__(self, engine, *, schema):
        self.engine = engine
        self.schema = schema
        self.tables = {table.name.removeprefix("catalog_"): table for table in build_metadata(schema).tables.values()}

    def table(self, name):
        return self.tables[name]

    def _record(self, conn, kind, key, *, revision=None):
        if kind == 'document':
            lock_document_transaction(conn, key)
        name, column = RECORDS[kind]
        table = self.table(name)
        row = conn.execute(select(table).where(table.c[column] == key).with_for_update()).mappings().first()
        if row is None:
            raise CatalogNotFound(f"{kind} not found")
        if revision is not None and row["revision"] != integer(revision, "revision"):
            raise CatalogConflict(f"{kind} changed; reload before applying")
        return dict(row)

    def _audit(self, conn, kind, key, before, after, actor):
        conn.execute(self.table("revisions").insert().values(
            record_kind=kind, record_key=str(key), actor=nonblank(actor, "actor"),
            before=snapshot(before), after=snapshot(after),
        ))

    def _protect(self, conn, kind, key, fields, actor):
        table = self.table("protections")
        for field in fields:
            statement = insert(table).values(record_kind=kind, record_key=str(key), field=field, actor=actor)
            conn.execute(statement.on_conflict_do_update(
                index_elements=[table.c.record_kind, table.c.record_key, table.c.field], set_={"actor": actor},
            ))

    def _protected(self, conn, kind, key):
        table = self.table("protections")
        return set(conn.execute(select(table.c.field).where(table.c.record_kind == kind, table.c.record_key == str(key))).scalars())

    def _update(self, conn, kind, key, before, changes, actor):
        name, column = RECORDS[kind]
        table = self.table(name)
        if kind == "entity" and "display_name" in changes:
            changes = {**changes, "normalized_name": changes["display_name"].casefold()}
        if kind == "collection" and "title" in changes:
            import re
            normalized = re.sub(r"[^0-9a-zа-яёәҗңөүһіғқҫ]+", " ", changes["title"].casefold()).strip()
            changes = {**changes, "normalized_title": normalized}
        if kind == "publication":
            changes = {**changes, "has_metadata": True, "metadata_present": True}
        row = conn.execute(table.update().where(table.c[column] == key).values(
            **changes, revision=before["revision"] + 1, updated_at=datetime.now(timezone.utc),
        ).returning(table)).mappings().one()
        self._audit(conn, kind, key, before, dict(row), actor)
        return dict(row)
