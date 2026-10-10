"""Catalog snapshots and atomic publication-based collection proposals."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.catalog.contracts import CatalogConflict
from app.catalog.repository import snapshot
from app.postgres_engine import configured_timeout_sql
from app.settings import configured_schema

COLLECTION_DISCOVERY_CONTRACT = "catalog.collection-discovery.v1"
_SOURCE = "catalog.collection_discovery"
_REQUIRED = {
    "publications": {"publication_id", "revision", "name", "work_type", "description", "date_published",
                     "collection_id", "has_metadata", "metadata_present", "merged_into_id"},
    "documents": {"md5", "publication_id", "revision"},
    "collections": {"collection_id", "title", "revision"},
    "genres": {"publication_id", "position", "value"},
    "subjects": {"publication_id", "position", "name", "term_code", "set_name"},
    "contributions": {"contribution_id", "publication_id", "role", "position", "nested_position",
                      "name_id", "entity_id", "resolution", "revision"},
    "names": {"name_id", "kind", "raw_name"},
    "entities": {"entity_id", "kind", "display_name", "approval", "status", "revision"},
    "proposals": {"proposal_id", "kind", "status", "display_name", "evidence", "field_changes",
                  "publication_id", "revision", "updated_at"},
    "revisions": {"revision_id", "record_kind", "record_key", "actor", "before", "after"},
}


def _fingerprint(value):
    return hashlib.sha256(json.dumps(snapshot(value), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class CollectionDiscoveryStore:
    def __init__(self, catalog):
        self.catalog = catalog
        self.engine = catalog.engine
        self.schema = catalog.schema

    def check(self):
        """Read-only deployment preflight; never apply migrations here."""
        required = {"catalog_" + name: columns for name, columns in _REQUIRED.items()}
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(configured_timeout_sql("statement_timeout", "preflight_timeout_seconds")))
            rows = conn.execute(text("""SELECT table_name,column_name FROM information_schema.columns
                WHERE table_schema=:schema AND table_name=ANY(:tables)"""),
                {"schema": self.schema, "tables": list(required)}).mappings()
            present = defaultdict(set)
            for row in rows:
                present[row["table_name"]].add(row["column_name"])
            missing = sorted(f"{name}.{column}" for name, columns in required.items()
                             for column in columns - present[name])
            if missing:
                raise RuntimeError("Catalog is incompatible with collection discovery; missing: " + ", ".join(missing))
            version_schema = configured_schema("migration_version_schema")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", version_schema):
                raise ValueError("Invalid migration version schema")
            revision = conn.execute(text(f'SELECT version_num FROM "{version_schema}".alembic_version')).scalar_one()
            if not re.fullmatch(r"\d{8}_\d{4}", str(revision)) or int(str(revision).split("_")[1]) < 62:
                raise RuntimeError("Catalog revision 20261008_0062 or later is required; apply migrations separately")
            if conn.execute(text("SELECT to_regprocedure('public.similarity(text,text)')")).scalar_one() is None:
                raise RuntimeError("Collection discovery requires the catalog's public pg_trgm extension")

    def _rows(self, conn, query, should_stop):
        query = query.replace("{catalog}", f'"{self.schema}".')
        with conn.execute(text(query), execution_options={"yield_per": 1000}) as result:
            for row in result.mappings():
                should_stop()
                yield dict(row)

    def _inventory(self, conn, should_stop):
        publications = {row["publication_id"]: row for row in self._rows(conn, """
            SELECT p.publication_id,p.revision,p.name,p.work_type,p.description,p.date_published,
                   p.collection_id,p.has_metadata,p.metadata_present
            FROM {catalog}catalog_publications p WHERE p.merged_into_id IS NULL
              AND EXISTS(SELECT 1 FROM {catalog}catalog_documents d WHERE d.publication_id=p.publication_id)
            ORDER BY p.publication_id
        """, should_stop)}
        for row in publications.values():
            row.update(documents=[], genres=[], subjects=[], credits=[])
        children = (
            ("documents", "SELECT md5,publication_id,revision FROM {catalog}catalog_documents ORDER BY md5"),
            ("genres", "SELECT publication_id,value FROM {catalog}catalog_genres ORDER BY publication_id,position"),
            ("subjects", """SELECT publication_id,name,term_code,set_name FROM {catalog}catalog_subjects
                             ORDER BY publication_id,position"""),
            ("credits", """SELECT c.publication_id,c.contribution_id,c.revision,c.role,c.position,c.nested_position,
                c.name_id,n.kind AS name_kind,n.raw_name,c.entity_id,c.resolution,
                e.revision AS entity_revision,e.kind AS entity_kind,e.display_name,e.approval,e.status,
                CASE WHEN c.resolution='confirmed' AND e.approval='confirmed' AND e.status='active'
                     THEN e.entity_id END AS confirmed_entity_id
                FROM {catalog}catalog_contributions c JOIN {catalog}catalog_names n USING(name_id)
                LEFT JOIN {catalog}catalog_entities e USING(entity_id)
                WHERE c.role IN ('author','publisher') ORDER BY c.contribution_id"""),
        )
        for key, query in children:
            for row in self._rows(conn, query, should_stop):
                if row["publication_id"] in publications:
                    publications[row["publication_id"]][key].append(row["value"] if key == "genres" else row)
        collections = list(self._rows(conn, """SELECT collection_id,title,revision
            FROM {catalog}catalog_collections ORDER BY collection_id""", should_stop))
        return {"publications": list(publications.values()), "collections": collections}

    def inventory(self, should_stop):
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            with conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text(configured_timeout_sql("statement_timeout", "query_timeout_seconds")))
                return self._inventory(conn, should_stop)

    def match_collections(self, features, signatures, should_stop):
        candidates = [{"publication_id": item["publication_id"], "title_core": item["title_core"]}
                      for item in features if item["eligible"] and item["collection_id"] is None and item["title_core"]]
        matches = []
        if not signatures:
            return matches
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(configured_timeout_sql("statement_timeout", "query_timeout_seconds")))
            for start in range(0, len(candidates), 1000):
                should_stop()
                rows = conn.execute(text("""
                    WITH scores AS (
                        SELECT f.publication_id,s.collection_id,max(public.similarity(f.title_core,s.value)) AS score
                        FROM jsonb_to_recordset(CAST(:features AS jsonb)) AS f(publication_id bigint,title_core text)
                        CROSS JOIN jsonb_to_recordset(CAST(:signatures AS jsonb)) AS s(collection_id bigint,value text)
                        GROUP BY f.publication_id,s.collection_id
                    ), ranked AS (
                        SELECT *,max(score) OVER (PARTITION BY publication_id) AS best FROM scores WHERE score>=0.72
                    ) SELECT publication_id,collection_id,score FROM ranked WHERE score=best
                      ORDER BY publication_id,collection_id
                """), {"features": json.dumps(candidates[start:start + 1000]),
                       "signatures": json.dumps(signatures)}).mappings()
                matches.extend(dict(row) for row in rows)
        targets = defaultdict(list)
        for row in matches:
            targets[row["publication_id"]].append(row["collection_id"])
        for row in matches:
            row["conflicting_collection_ids"] = [key for key in targets[row["publication_id"]] if key != row["collection_id"]]
        return matches

    def _lock_inputs(self, conn, inventory, should_stop):
        publications = inventory["publications"]
        credits = [credit for row in publications for credit in row["credits"]]
        locks = (
            ("publications", "publication_id", [row["publication_id"] for row in publications]),
            ("documents", "md5", [doc["md5"] for row in publications for doc in row["documents"]]),
            ("contributions", "contribution_id", [row["contribution_id"] for row in credits]),
            ("names", "name_id", [row["name_id"] for row in credits]),
            ("entities", "entity_id", [row["entity_id"] for row in credits if row["entity_id"] is not None]),
            ("collections", "collection_id", [row["collection_id"] for row in inventory["collections"]]),
        )
        for name, key, ids in locks:
            should_stop()
            table = self.catalog.table(name)
            ids = sorted(set(ids))
            for start in range(0, len(ids), 1000):
                should_stop()
                conn.execute(select(table.c[key]).where(table.c[key].in_(ids[start:start + 1000]))
                             .order_by(table.c[key]).with_for_update(read=True)).all()

    def _payload(self, candidate):
        members = sorted(candidate["members"], key=lambda row: row["publication_id"])
        target = candidate["target"]
        key = _fingerprint({"contract_version": COLLECTION_DISCOVERY_CONTRACT,
                            "proposal_type": candidate["proposal_type"],
                            "target_collection_id": target["collection_id"] if target else None,
                            "publication_ids": [row["publication_id"] for row in members]})
        payload = {"contract_version": COLLECTION_DISCOVERY_CONTRACT, "candidate_key": key,
                   "proposal_type": candidate["proposal_type"], "target_collection": target,
                   "deterministic_score": candidate["score"], "evidence": candidate["evidence"],
                   "members": members}
        payload["input_fingerprint"] = _fingerprint(payload)
        return {"source": _SOURCE, "collection": payload}

    def refresh(self, inventory, candidates, *, should_stop, log):
        """Refresh only this contract's undecided proposals; rollback on interruption."""
        table = self.catalog.table("proposals")
        counts = {"proposals_created": 0, "proposals_updated": 0, "proposals_reused": 0,
                  "reviewed_proposals_preserved": 0, "proposals_superseded": 0,
                  "proposal_ids": [], "superseded_proposal_ids": []}
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            conn.execute(text(configured_timeout_sql("lock_timeout", "lock_timeout_seconds")))
            conn.execute(text(configured_timeout_sql("statement_timeout", "query_timeout_seconds")))
            conn.execute(text("SELECT set_config('manzara.catalog_actor','task',true)"))
            acquired = conn.execute(text("SELECT pg_try_advisory_xact_lock(hashtext(:scope))"),
                                    {"scope": "collection-discovery:" + self.schema}).scalar_one()
            if not acquired:
                raise CatalogConflict("Another collection discovery is refreshing proposals; retry after it finishes")
            self._lock_inputs(conn, inventory, should_stop)
            if _fingerprint(self._inventory(conn, should_stop)) != _fingerprint(inventory):
                raise CatalogConflict("Collection discovery inputs changed; rerun discovery. Existing proposals are preserved")
            previous = [dict(row) for row in conn.execute(select(table).where(
                table.c.kind == "collection", table.c.evidence["source"].astext == _SOURCE,
                table.c.evidence["collection"]["contract_version"].astext == COLLECTION_DISCOVERY_CONTRACT,
            ).order_by(table.c.proposal_id).with_for_update()).mappings()]
            by_key = {row["evidence"]["collection"]["candidate_key"]: row for row in previous}
            generated = set()
            for candidate in candidates:
                should_stop()
                payload = self._payload(candidate)
                key = payload["collection"]["candidate_key"]
                generated.add(key)
                before = by_key.get(key)
                if before and before["status"] not in {"pending", "superseded"}:
                    counts["reviewed_proposals_preserved"] += 1
                    continue
                if before and before["status"] == "pending" and before["evidence"] == payload and before["display_name"] == candidate["title"]:
                    counts["proposals_reused"] += 1
                    counts["proposal_ids"].append(before["proposal_id"])
                    continue
                values = {"status": "pending", "display_name": candidate["title"], "evidence": payload}
                if before:
                    values.update(revision=before["revision"] + 1, updated_at=datetime.now(timezone.utc))
                    statement = table.update().where(table.c.proposal_id == before["proposal_id"]).values(**values)
                else:
                    statement = table.insert().values(kind="collection", field_changes={}, **values)
                after = dict(conn.execute(statement.returning(table)).mappings().one())
                self.catalog._audit(conn, "proposal", after["proposal_id"], before, after, "task")
                counts["proposals_updated" if before else "proposals_created"] += 1
                counts["proposal_ids"].append(after["proposal_id"])
            for before in previous:
                should_stop()
                if before["status"] != "pending" or before["evidence"]["collection"]["candidate_key"] in generated:
                    continue
                after = dict(conn.execute(table.update().where(table.c.proposal_id == before["proposal_id"])
                    .values(status="superseded", revision=before["revision"] + 1, updated_at=datetime.now(timezone.utc))
                    .returning(table)).mappings().one())
                self.catalog._audit(conn, "proposal", after["proposal_id"], before, after, "task")
                counts["proposals_superseded"] += 1
                counts["superseded_proposal_ids"].append(after["proposal_id"])
            should_stop()
        log(f"Collection proposals committed: created={counts['proposals_created']} updated={counts['proposals_updated']} "
            f"reused={counts['proposals_reused']} superseded={counts['proposals_superseded']}")
        for start in range(0, len(counts["proposal_ids"]), 100):
            log(f"Collection proposal IDs: {counts['proposal_ids'][start:start + 100]}")
        for start in range(0, len(counts["superseded_proposal_ids"]), 100):
            log(f"Superseded collection proposal IDs: {counts['superseded_proposal_ids'][start:start + 100]}")
        return counts
