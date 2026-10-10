"""Catalog-native publisher inventories and proposal-generation checkpoints."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re

from sqlalchemy import BigInteger, Column, MetaData, Table, Text, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB

from app.catalog.contracts import CatalogConflict, integer

PUBLISHER_ANALYSIS_CONTRACT = "catalog.publisher-clusters.v1"


def _json(value):
    return json.loads(json.dumps(value, default=lambda item: item.isoformat()))


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _identity(entry):
    """Document counts are observations, not reviewed identity facts."""
    return {key: entry[key] for key in ("key", "display_name", "aliases", "is_new", "members", "linked_names")}


class PublisherCatalogStore:
    def __init__(self, catalog):
        self.catalog = catalog
        self.engine = catalog.engine
        self.schema = catalog.schema
        self.analyses = Table(
            "publisher_merge_analyses", MetaData(schema=self.schema),
            Column("analysis_id", BigInteger, primary_key=True),
            *[Column(name, Text) for name in ("fingerprint", "scope", "state", "created_at", "updated_at")],
            *[Column(name, JSONB) for name in ("inventory", "metadata", "response")],
        )

    def check(self):
        """Inspect deployment read-only; never install or repair schema here."""
        required = {
            table.name: set(table.c.keys()) for table in (
                self.analyses, *(self.catalog.table(name) for name in (
                    "publications", "documents", "contributions", "credit_groups", "entities",
                    "entity_roles", "names", "aliases", "alias_reviews", "proposals",
                    "proposal_members", "separations", "revisions",
                )),
            )
        }
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text("SET LOCAL statement_timeout = '5s'"))
            rows = conn.execute(text("""SELECT table_name,column_name FROM information_schema.columns
                WHERE table_schema=:schema AND table_name=ANY(:tables)"""),
                {"schema": self.schema, "tables": list(required)}).mappings()
            present = {}
            for row in rows:
                present.setdefault(row["table_name"], set()).add(row["column_name"])
            missing = sorted(f"{table}.{column}" for table, columns in required.items()
                             for column in columns - present.get(table, set()))
            if missing:
                raise RuntimeError("Catalog is incompatible with publisher clustering; missing: " + ", ".join(missing))
            version_schema = os.environ.get("MANZARA_ALEMBIC_VERSION_SCHEMA") or self.schema
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", version_schema):
                raise ValueError("Invalid migration version schema")
            revision = conn.execute(text(
                f'SELECT version_num FROM "{version_schema}".alembic_version_manzara'
            )).scalar_one()
            if not re.fullmatch(r"\d{8}_\d{4}", str(revision)) or int(str(revision).split("_")[1]) < 62:
                raise RuntimeError("Catalog revision 20261008_0062 or later is required; apply migrations separately.")

    def _inventory(self, conn, *, lock=False):
        table = self.catalog.table
        entities, roles, credits = table("entities"), table("entity_roles"), table("contributions")
        names, reviews, publications, documents = (
            table("names"), table("alias_reviews"), table("publications"), table("documents")
        )
        # Preserve the retired canonical projection's publisher classification.
        statement = select(entities).where(entities.c.status == "active", or_(
            entities.c.kind != "person",
            select(roles.c.entity_id).where(roles.c.entity_id == entities.c.entity_id, roles.c.role == "publisher").exists(),
            select(credits.c.entity_id).where(credits.c.entity_id == entities.c.entity_id, credits.c.role == "publisher").exists(),
        )).order_by(entities.c.entity_id)
        if lock:
            statement = statement.with_for_update()
        active = {row["entity_id"]: _json(dict(row)) for row in conn.execute(statement).mappings()}
        linked = select(reviews.c.name_id, reviews.c.entity_id, names.c.raw_name).join(names).where(
            reviews.c.entity_type == "publisher", reviews.c.decision_status == "linked",
            reviews.c.entity_id.in_(active),
        ).order_by(reviews.c.name_id)
        if lock:
            linked = linked.with_for_update(of=reviews)
        associations = {key: [] for key in active}
        for row in conn.execute(linked).mappings():
            associations[row["entity_id"]].append({"name_id": row["name_id"], "raw_name": row["raw_name"]})
        observed = conn.execute(select(names, documents.c.md5).select_from(credits)
            .join(names).join(publications, publications.c.publication_id == credits.c.publication_id)
            .join(documents, documents.c.publication_id == publications.c.publication_id)
            .where(credits.c.role == "publisher", publications.c.inclusion == "included",
                   publications.c.has_metadata.is_(True), publications.c.metadata_present.is_(True))
            .order_by(names.c.name_id, documents.c.md5)).mappings()
        mentions, observed_names = {}, {}
        for row in observed:
            spelling = row["raw_name"].strip()
            if not spelling:
                continue
            mentions.setdefault(spelling, set()).add(row["md5"])
            observed_names.setdefault(spelling, {})[row["name_id"]] = {
                "name_id": row["name_id"], "kind": row["kind"], "raw_name": row["raw_name"],
            }
        if lock:
            ids = {item["name_id"] for items in associations.values() for item in items}
            ids.update(row["name_id"] for items in observed_names.values() for row in items.values())
            conn.execute(select(names.c.name_id).where(names.c.name_id.in_(ids))
                         .order_by(names.c.name_id).with_for_update()).all()
        items, covered = [], set()
        for entity_id, entity in active.items():
            aliases = sorted({entity["display_name"], *(row["raw_name"] for row in associations[entity_id])})
            covered.update(aliases)
            doc_ids = set().union(*(mentions.get(name, set()) for name in aliases))
            items.append({"key": f"entity:{entity_id}", "entity_id": entity_id, "raw_name": None,
                          "display_name": entity["display_name"], "aliases": aliases,
                          "document_count": len(doc_ids), "is_new": False,
                          "linked_names": associations[entity_id],
                          "members": [{"entity_id": entity_id, "reviewed_revision": entity["revision"], "snapshot": entity}]})
        for spelling in sorted(set(mentions) - covered):
            members = [{"name_id": name_id, "reviewed_revision": None, "snapshot": row}
                       for name_id, row in sorted(observed_names[spelling].items())]
            items.append({"key": f"name:{members[0]['name_id']}", "entity_id": None, "raw_name": spelling,
                          "display_name": spelling, "aliases": [], "document_count": len(mentions[spelling]),
                          "is_new": True, "linked_names": [], "members": members})
        return sorted(items, key=lambda row: row["key"])

    def inventory(self):
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            return self._inventory(conn)

    def _separations(self, conn, inventory):
        """Honor decisions for both explicit members and suppressed alias spellings."""
        mapping, spellings = {}, {}
        for entry in inventory:
            for member in entry["members"]:
                for field in ("entity_id", "name_id"):
                    if field in member:
                        key = f"{field.removesuffix('_id')}:{member[field]}"
                        mapping.setdefault(key, set()).add(entry["key"])
            if not entry["is_new"]:
                for spelling in entry["aliases"]:
                    spellings.setdefault(spelling, set()).add(entry["key"])
        names = self.catalog.table("names")
        for row in conn.execute(select(names.c.name_id, names.c.raw_name).where(
            names.c.raw_name.in_(spellings),
        )).mappings():
            mapping.setdefault(f"name:{row['name_id']}", set()).update(spellings[row["raw_name"]])
        rows = conn.execute(select(self.catalog.table("separations"))).mappings()
        pairs = set()
        for row in rows:
            if row["left_key"] in mapping and row["right_key"] in mapping:
                for left in mapping[row["left_key"]]:
                    for right in mapping[row["right_key"]]:
                        # Equal endpoints expose a contradiction inside one inventory entry.
                        pairs.add(tuple(sorted((left, right))))
        return [list(pair) for pair in sorted(pairs)]

    def state(self, inventory=None):
        compatible = self.analyses.c.metadata["contract_version"].astext == PUBLISHER_ANALYSIS_CONTRACT
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            successful = conn.execute(select(self.analyses.c.analysis_id).where(
                compatible, self.analyses.c.state.in_(("checkpointed", "imported"))).limit(1)).first()
            checkpoint = conn.execute(select(self.analyses).where(
                compatible, self.analyses.c.state == "checkpointed").order_by(self.analyses.c.analysis_id).limit(1)).mappings().first()
            source = inventory if inventory is not None else (checkpoint["inventory"] if checkpoint else [])
            return {"successful": bool(successful), "checkpoint": dict(checkpoint) if checkpoint else None,
                    "separations": self._separations(conn, source)}

    def get_analysis(self, analysis_id):
        integer(analysis_id, "analysis_id")
        with self.engine.begin() as conn:
            row = conn.execute(select(self.analyses).where(self.analyses.c.analysis_id == analysis_id)).mappings().first()
        if row is None or row["metadata"].get("contract_version") != PUBLISHER_ANALYSIS_CONTRACT:
            raise ValueError("No publisher analysis with the current catalog contract; historical analyses are preserved.")
        return dict(row)

    def create_analysis(self, inventory, fingerprint, scope, metadata):
        now = datetime.now(timezone.utc).isoformat()
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            conn.execute(self.analyses.update().where(
                self.analyses.c.metadata["contract_version"].astext == PUBLISHER_ANALYSIS_CONTRACT,
                self.analyses.c.state == "generating",
            ).values(state="failed", updated_at=now))
            return conn.execute(self.analyses.insert().values(
                inventory=inventory, fingerprint=fingerprint, scope=scope,
                metadata={**metadata, "contract_version": PUBLISHER_ANALYSIS_CONTRACT},
                state="generating", created_at=now, updated_at=now,
            ).returning(self.analyses.c.analysis_id)).scalar_one()

    def checkpoint(self, analysis_id, groups, metadata):
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            result = conn.execute(self.analyses.update().where(
                self.analyses.c.analysis_id == analysis_id, self.analyses.c.state == "generating",
                self.analyses.c.metadata["contract_version"].astext == PUBLISHER_ANALYSIS_CONTRACT,
            ).values(response=groups, metadata={**metadata, "contract_version": PUBLISHER_ANALYSIS_CONTRACT},
                     state="checkpointed", updated_at=datetime.now(timezone.utc).isoformat()))
            if result.rowcount != 1:
                raise CatalogConflict("Publisher analysis changed before response checkpointing.")

    def reject_checkpoint(self, analysis_id, reason):
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            conn.execute(self.analyses.update().where(
                self.analyses.c.analysis_id == analysis_id, self.analyses.c.state == "checkpointed",
                self.analyses.c.metadata["contract_version"].astext == PUBLISHER_ANALYSIS_CONTRACT,
            ).values(state="failed", metadata=self.analyses.c.metadata.concat({"failure_context": reason}),
                     updated_at=datetime.now(timezone.utc).isoformat()))

    def import_analysis(self, analysis_id):
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            conn.execute(text("SELECT set_config('manzara.catalog_actor', 'task', true)"))
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('catalog-identity-review'))"))
            analysis = conn.execute(select(self.analyses).where(
                self.analyses.c.analysis_id == analysis_id).with_for_update()).mappings().one()
            if analysis["metadata"].get("contract_version") != PUBLISHER_ANALYSIS_CONTRACT:
                raise ValueError("Historical publisher analyses cannot be imported by the CLI task.")
            if analysis["state"] == "imported":
                return {"count": 0, "proposal_ids": []}
            if analysis["state"] != "checkpointed":
                raise ValueError("No completed publisher response checkpoint")
            inventory = analysis["inventory"]
            current = {row["key"]: row for row in self._inventory(conn, lock=True)}
            for entry in inventory:
                if entry["key"] not in current or _identity(current[entry["key"]]) != _identity(entry):
                    raise CatalogConflict("Publisher identities or linked names changed; start another analysis. Existing proposals and decisions are preserved.")
            pairs = self._separations(conn, inventory)
            proposals, members = self.catalog.table("proposals"), self.catalog.table("proposal_members")
            entries = {row["key"]: row for row in inventory}
            ids = []
            for group in analysis["response"]:
                if any(set(pair).issubset(group["member_ids"]) for pair in pairs):
                    raise CatalogConflict("Publisher response conflicts with a current separation decision; start another analysis.")
                selected = [entries[key] for key in sorted(group["member_ids"])]
                fingerprint = _fingerprint({
                    "members": [_identity(entry) for entry in selected],
                    "kind": group["kind"], "proposed_name": group["proposed_name"],
                })
                existing = conn.execute(select(proposals.c.proposal_id).where(
                    proposals.c.evidence["source"].astext == "catalog.publisher_analysis",
                    proposals.c.evidence["publisher"]["contract_version"].astext == PUBLISHER_ANALYSIS_CONTRACT,
                    proposals.c.evidence["publisher"]["fingerprint"].astext == fingerprint,
                    proposals.c.status.in_(("pending", "deferred")),
                ).limit(1)).first()
                if existing:
                    continue
                payload = {"analysis_id": analysis_id, "fingerprint": fingerprint, "proposal": group,
                           "members": selected, "contract_version": PUBLISHER_ANALYSIS_CONTRACT}
                proposal = dict(conn.execute(proposals.insert().values(
                    kind="identity", status="pending", display_name=group["proposed_name"],
                    evidence={"source": "catalog.publisher_analysis", "publisher": payload},
                ).returning(proposals)).mappings().one())
                for entry in selected:
                    for member in entry["members"]:
                        conn.execute(members.insert().values(proposal_id=proposal["proposal_id"], **member))
                self.catalog._audit(conn, "proposal", proposal["proposal_id"], None, proposal, "task")
                ids.append(proposal["proposal_id"])
            conn.execute(self.analyses.update().where(self.analyses.c.analysis_id == analysis_id).values(
                state="imported", updated_at=datetime.now(timezone.utc).isoformat()))
            return {"count": len(ids), "proposal_ids": ids}
