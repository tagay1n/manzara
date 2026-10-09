"""Explicit edition grouping without inferred bibliographic winners."""

from sqlalchemy import select

from app.catalog.contracts import CatalogConflict, integer, validate_patch
from app.document_operation_lock import lock_document_transaction


class GroupingStore:
    def merge_publications(self, target_id, source_revisions, *, revision, resolutions, actor):
        if not isinstance(source_revisions, dict) or not source_revisions or target_id in source_revisions:
            raise ValueError("grouping needs distinct source publications")
        if len(source_revisions) > 100 or not isinstance(resolutions, dict):
            raise ValueError("publication grouping is bounded and requires explicit resolutions")
        with self.engine.begin() as conn:
            conn.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('catalog-publication-grouping'))")
            docs = self.table('documents')
            for md5 in conn.execute(select(docs.c.md5).where(docs.c.publication_id.in_(source_revisions)).order_by(docs.c.md5)).scalars():
                lock_document_transaction(conn, md5)
            rows = {key: self._record(conn, "publication", key, revision=expected) for key, expected in sorted({target_id: revision, **source_revisions}.items())}
            if any(row["merged_into_id"] is not None for row in rows.values()):
                raise CatalogConflict("publication was already grouped")
            fields = {"name", "work_type", "description", "edition", "date_published", "page_count", "languages",
                      "inclusion", "classification_id", "collection_id", "audience_array"}
            if set(resolutions) - fields - {"contributions", "identifiers", "genres", "subjects", "audiences", "references", "reference_authors"}:
                raise ValueError("unsupported grouping resolution")
            editable = {field: value for field, value in resolutions.items() if field in fields - {"audience_array"}}
            if editable:
                validate_patch("publication", {"revision": revision, **editable})
            changes = {}
            for field in fields:
                values = [row[field] for row in rows.values() if row[field] is not None]
                if any(value != values[0] for value in values[1:]):
                    if field not in resolutions:
                        raise ValueError(f"publication conflict requires resolution: {field}")
                    changes[field] = resolutions[field]
                elif rows[target_id][field] is None and values:
                    changes[field] = values[0]
            relations = {"contributions", "identifiers", "genres", "subjects", "audiences", "references", "reference_authors"}
            # Relation sets must be selected explicitly when editions disagree.
            for name in relations:
                table = self.table(name)
                contents = {key: [dict(item) for item in conn.execute(select(table).where(table.c.publication_id == key)).mappings()] for key in rows}
                def compare(items):
                    return [{k: v for k, v in item.items() if k not in {"publication_id", "contribution_id", "revision", "updated_at"}} for item in items]
                unique = [compare(contents[key]) for key in rows if contents[key]]
                if unique and any(value != unique[0] for value in unique[1:]):
                    selected = resolutions.get(name)
                    if selected not in rows:
                        raise ValueError(f"publication conflict requires source publication selection: {name}")
                else:
                    selected = next((key for key in rows if contents[key]), target_id)
                if selected != target_id:
                    conn.execute(table.delete().where(table.c.publication_id == target_id))
                    conn.execute(table.update().where(table.c.publication_id == selected).values(publication_id=target_id))
                for source in source_revisions:
                    if source != selected:
                        conn.execute(table.delete().where(table.c.publication_id == source))
            docs = self.table("documents")
            conn.execute(docs.update().where(docs.c.publication_id.in_(source_revisions)).values(publication_id=target_id, revision=docs.c.revision + 1))
            for key in source_revisions:
                self._update(conn, "publication", key, rows[key], {"merged_into_id": target_id}, actor)
            self._protect(conn, "publication", target_id, fields | {"credits", "identifiers", "genres", "subjects", "audiences", "based_on"}, actor)
            return self._update(conn, "publication", target_id, rows[target_id], changes, actor)

    def assign_classification(self, publication_id, node_id, *, revision, actor):
        integer(node_id, "node_id")
        with self.engine.begin() as conn:
            self._record(conn, "classification_node", node_id)
            table = self.table("classifications")
            classification_id = conn.execute(select(table.c.classification_id).where(table.c.node_id == node_id)).scalar()
            if classification_id is None:
                classification_id = conn.execute(table.insert().values(node_id=node_id).returning(table.c.classification_id)).scalar_one()
            pub = self._record(conn, "publication", publication_id, revision=revision)
            subjects = self.table("subjects")
            conn.execute(subjects.delete().where(subjects.c.publication_id == publication_id,
                subjects.c.set_name.in_(["DDC", "CategoryPath"])))
            self._protect(conn, "publication", publication_id, {"classification_id"}, actor)
            return self._update(conn, "publication", publication_id, pub, {"classification_id": classification_id}, actor)
