"""Shared transactional entry point for both processing and administration."""

from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import String, cast, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from app.catalog.contracts import (
    CatalogConflict, CatalogNotFound, SEARCH_FIELDS, integer, nonblank, parse_filters, validate_patch,
)
from app.catalog.schema import build_metadata
from app.catalog.metadata_store import MetadataStore
from app.catalog.identities import IdentityStore
from app.catalog.previews import PreviewStore
from app.catalog.grouping import GroupingStore
from app.catalog.personality_normalization import PersonalityNormalizationStore


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


class CatalogRepository(MetadataStore, IdentityStore, PreviewStore, GroupingStore, PersonalityNormalizationStore):
    def __init__(self, engine, *, schema="monocorpus"):
        self.engine = engine
        self.schema = schema
        self.tables = {table.name.removeprefix("catalog_"): table for table in build_metadata(schema).tables.values()}

    def table(self, name):
        return self.tables[name]

    def _record(self, conn, kind, key, *, revision=None):
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

    def get(self, kind, key):
        if kind not in RECORDS:
            raise ValueError("unsupported record kind")
        with self.engine.begin() as conn:
            return self._record(conn, kind, key)

    def patch(self, kind, key, payload, *, actor):
        values = validate_patch(kind, payload)
        revision = values.pop("revision")
        with self.engine.begin() as conn:
            if kind == "classification_node":
                conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"catalog-taxonomy:{self.schema}"})
            before = self._record(conn, kind, key, revision=revision)
            if kind == "classification_node":
                self._validate_parent(conn, key, values.get("parent_id", before["parent_id"]), before["ddc"])
                nodes = self.table("classification_nodes")
                descendants = select(nodes.c.node_id).where(nodes.c.node_id == key).cte(recursive=True)
                descendants = descendants.union_all(select(nodes.c.node_id).join(descendants, nodes.c.parent_id == descendants.c.node_id))
                classifications, publications = self.table("classifications"), self.table("publications")
                affected = select(publications.c.publication_id).join(classifications).where(classifications.c.node_id.in_(select(descendants.c.node_id)))
                self._touch_publications(conn, set(conn.execute(affected).scalars()), actor)
            if kind == "entity" and "display_name" in values:
                self._retain_alias(conn, key, before["kind"], before["display_name"])
                credits = self.table("contributions")
                self._touch_publications(conn, set(conn.execute(select(credits.c.publication_id).where(credits.c.entity_id == key)).scalars()), actor)
            self._protect(conn, kind, key, values, actor)
            return self._update(conn, kind, key, before, values, actor)

    def create_document(self, md5, *, mime_type=None, complete=True, restricted=False, actor):
        import re
        from app.catalog.contracts import boolean

        if not isinstance(md5, str) or not re.fullmatch(r"[0-9a-f]{32}", md5):
            raise ValueError("md5 must be 32 lowercase hexadecimal characters")
        with self.engine.begin() as conn:
            publication_id = conn.execute(self.table("publications").insert().returning(self.table("publications").c.publication_id)).scalar_one()
            table = self.table("documents")
            row = dict(conn.execute(table.insert().values(
                md5=md5, publication_id=publication_id, mime_type=mime_type,
                complete=boolean(complete, "complete"), restricted=boolean(restricted, "restricted"),
            ).returning(table)).mappings().one())
            self._audit(conn, "document", md5, None, row, actor)
            return row

    def _search(self, conn, resource, statement, fields, filters, *, page, page_size):
        page = integer(page, "page")
        page_size = integer(page_size, "page_size")
        if page_size > 100:
            raise ValueError("page_size must not exceed 100")
        conn.execute(text("SET LOCAL statement_timeout = '3s'"))
        for item in parse_filters(resource, filters or []):
            # Columns originate only from this backend-owned registry.
            statement = statement.where(cast(fields[item["field"]], String).op("~*" if item["ignore_case"] else "~")(item["pattern"]))
        try:
            total = conn.execute(select(func.count()).select_from(statement.order_by(None).subquery())).scalar_one()
            rows = conn.execute(statement.limit(page_size).offset((page - 1) * page_size)).mappings().all()
        except DBAPIError as exc:
            code = getattr(exc.orig, "pgcode", None)
            if code == "2201B":
                raise ValueError("invalid regular expression") from None
            if code == "57014":
                raise ValueError("search timed out; narrow the regular expression or filters") from None
            raise
        return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    def list_documents(self, *, inclusion="all", filters=None, page=1, page_size=25):
        if inclusion not in {"all", "included", "excluded", "pending"}:
            raise ValueError("unsupported inclusion filter")
        d, p, locations = self.table("documents"), self.table("publications"), self.table("locations")
        source_path = select(locations.c.source_path).where(locations.c.md5 == d.c.md5, locations.c.provider == "yandex").limit(1).scalar_subquery()
        statement = select(d, p.c.name.label("title"), p.c.inclusion,
                           p.c.languages.label("languages"), source_path.label("source_path")).join(p, p.c.publication_id == d.c.publication_id).order_by(d.c.md5)
        if inclusion != "all":
            statement = statement.where(p.c.inclusion == inclusion)
        fields = {"md5": d.c.md5, "title": p.c.name, "mime_type": d.c.mime_type,
                  "inclusion": p.c.inclusion, "source_path": source_path,
                  "language": func.array_to_string(p.c.languages, ",")}
        with self.engine.begin() as conn:
            return self._search(conn, "documents", statement, fields, filters, page=page, page_size=page_size)

    def list_records(self, kind, *, filters=None, page=1, page_size=25, approval=None, role=None):
        name, column = RECORDS[kind]
        table = self.table(name)
        resource = {"publication": "publications", "entity": "entities", "collection": "collections"}.get(kind)
        if not resource:
            raise ValueError("unsupported list resource")
        statement = select(table).order_by(table.c[column])
        if kind == "publication":
            statement = statement.where(table.c.merged_into_id.is_(None))
        if kind == "entity":
            statement = statement.where(table.c.status == "active")
            if approval is not None:
                if approval not in {"confirmed", "unconfirmed"}:
                    raise ValueError("unsupported approval")
                statement = statement.where(table.c.approval == approval)
            if role is not None:
                roles = self.table("entity_roles")
                credits = self.table("contributions")
                condition = (table.c.kind == "person") if role == "personality" else (
                    select(roles).where(roles.c.entity_id == table.c.entity_id, roles.c.role == role).exists()
                    | select(credits).where(credits.c.entity_id == table.c.entity_id, credits.c.role == role).exists())
                statement = statement.where(condition)
        with self.engine.begin() as conn:
            return self._search(conn, resource, statement, {key: table.c[key] for key in SEARCH_FIELDS[resource]}, filters, page=page, page_size=page_size)

    def create_collection(self, title, *, actor, notes=None):
        with self.engine.begin() as conn:
            table = self.table("collections")
            row = dict(conn.execute(table.insert().values(title=nonblank(title, "title"), notes=notes).returning(table)).mappings().one())
            self._audit(conn, "collection", row["collection_id"], None, row, actor)
            return row

    def list_names(self, *, kind=None, role=None, unresolved=False, filters=None, page=1, page_size=25):
        names, credits = self.table("names"), self.table("contributions")
        statement = select(names).order_by(names.c.name_id)
        if kind is not None:
            if kind not in {"person", "organization", "unknown"}:
                raise ValueError("unsupported name kind")
            statement = statement.where(names.c.kind == kind)
        mentions = select(credits).where(credits.c.name_id == names.c.name_id)
        if role is not None:
            mentions = mentions.where(credits.c.role == role)
        if unresolved:
            mentions = mentions.where(credits.c.entity_id.is_(None))
        if role is not None or unresolved:
            statement = statement.where(mentions.exists())
        with self.engine.begin() as conn:
            return self._search(conn, "names", statement, {"raw_name": names.c.raw_name, "kind": names.c.kind}, filters, page=page, page_size=page_size)

    def list_classification_nodes(self, *, filters=None, page=1, page_size=25):
        table = self.table("classification_nodes")
        with self.engine.begin() as conn:
            return self._search(conn, "classifications", select(table).order_by(table.c.node_id),
                                {key: table.c[key] for key in SEARCH_FIELDS["classifications"]}, filters, page=page, page_size=page_size)

    def create_classification_node(self, ddc, label_en, *, parent_id=None, label_tt=None, actor):
        with self.engine.begin() as conn:
            self._validate_parent(conn, None, parent_id, ddc)
            table = self.table("classification_nodes")
            row = dict(conn.execute(table.insert().values(ddc=nonblank(ddc, "ddc"), label_en=nonblank(label_en, "label_en"), label_tt=label_tt, parent_id=parent_id).returning(table)).mappings().one())
            self._audit(conn, "classification_node", row["node_id"], None, row, actor)
            return row

    def _validate_parent(self, conn, node_id, parent_id, ddc):
        # Serialize topology changes; row locks alone cannot prevent concurrent cycles.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"catalog-taxonomy:{self.schema}"})
        visited = {node_id}
        while parent_id is not None:
            if parent_id in visited:
                raise ValueError("classification cycle")
            visited.add(parent_id)
            row = self._record(conn, "classification_node", integer(parent_id, "parent_id"))
            if row["ddc"] != ddc:
                raise ValueError("classification parent must use the same DDC")
            parent_id = row["parent_id"]

    def classification_path(self, node_id):
        with self.engine.begin() as conn:
            return self._classification_path(conn, node_id)

    def _classification_path(self, conn, node_id):
        rows, seen = [], set()
        while node_id is not None:
            if node_id in seen:
                raise ValueError("classification cycle")
            seen.add(node_id)
            row = self._record(conn, "classification_node", node_id)
            rows.append(row)
            node_id = row["parent_id"]
        return {"path_en": [row["label_en"] for row in reversed(rows)],
                "path_tt": [row["label_tt"] for row in reversed(rows)], "ddc": rows[0]["ddc"]}

    def revisions(self, kind, key, *, limit=100):
        integer(limit, "limit")
        table = self.table("revisions")
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(table).where(table.c.record_kind == kind, table.c.record_key == str(key)).order_by(table.c.revision_id.desc()).limit(min(limit, 100))).mappings()]
