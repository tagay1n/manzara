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
)
from app.catalog.metadata_store import MetadataStore
from app.catalog.personality_normalization import PersonalityNormalizationStore
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
    """Serialize snapshot timestamps without leaking object representations."""
    return json.loads(json.dumps(value, default=lambda item: item.isoformat()))


class CatalogRepository(MetadataStore, PersonalityNormalizationStore):
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

    def _update(self, conn, kind, key, before, changes):
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
        return dict(row)

    def validate_publication_metadata(self, publication_id, *, revision, actor):
        """Validate and permanently protect a reviewed publication from automation."""
        from app.catalog.contracts import nonblank
        from app.catalog.schema_org import metadata_contract_issues
        from app.postgres_engine import configured_timeout_sql
        from sqlalchemy import text

        integer(publication_id, 'publication_id')
        actor = nonblank(actor, 'admin actor')
        with self.engine.begin() as conn:
            conn.execute(text(configured_timeout_sql('lock_timeout', 'lock_timeout_seconds')))
            publication = self._record(conn, 'publication', publication_id, revision=revision)
            if publication['merged_into_id'] is not None:
                raise CatalogConflict('Publication has been merged')
            if not publication['metadata_present']:
                raise CatalogConflict('Extract metadata before admin validation')
            proposals = self.table('proposals')
            if conn.execute(select(proposals.c.proposal_id).where(
                    proposals.c.publication_id == publication_id, proposals.c.kind == 'metadata',
                    proposals.c.status.in_(['pending', 'deferred']))).first():
                raise CatalogConflict('Resolve pending metadata proposals before validation')
            documents = self.table('documents')
            md5s = conn.execute(select(documents.c.md5).where(
                documents.c.publication_id == publication_id).order_by(documents.c.md5)).scalars().all()
            if not md5s:
                raise CatalogConflict('Publication has no evidence documents')
            for md5 in md5s:
                schema = conn.execute(text('SELECT catalog_schema_org(:md5)'), {'md5': md5}).scalar_one()
                if metadata_contract_issues(schema):
                    raise CatalogConflict('Publication metadata does not satisfy the metadata contract')
            if publication['metadata_review_order'] == -1:
                return publication
            result = self._update(conn, 'publication', publication_id, publication, {'metadata_review_order': -1})
            conn.execute(self.table('evidence').insert().values(record_kind='publication',
                record_key=str(publication_id), source=actor,
                payload={'operation': 'admin_metadata_validation', 'revision': result['revision']}))
            return result
