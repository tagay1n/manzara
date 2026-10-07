"""Transfer outstanding identity decisions without treating AI output as approval."""

from itertools import combinations
from collections import defaultdict, deque
import json

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert


def import_identity_reviews(catalog, conn, source, *, actor):
    names, credits = catalog.table("names"), catalog.table("contributions")
    aliases, roles = catalog.table("aliases"), catalog.table("entity_roles")
    from app.catalog.importer import _copy_rows
    from app.catalog.contracts import CatalogNotFound

    entity_table = catalog.table("entities")
    entity_rows = {row["entity_id"]: dict(row) for row in conn.execute(select(entity_table).with_for_update()).mappings()}
    name_rows = {(row["kind"], row["raw_name"]): row["name_id"] for row in conn.execute(select(names)).mappings()}
    pending_names, pending_proposals, pending_members = [], [], []
    reserved = defaultdict(deque)

    def identifier(table, column):
        if not reserved[table]:
            relation = f'"{catalog.schema}"."catalog_{table}"'
            reserved[table].extend(conn.execute(text("SELECT nextval(pg_get_serial_sequence(:relation,:column)) FROM generate_series(1,256)"),
                {"relation": relation, "column": column}).scalars())
        return reserved[table].popleft()

    publisher_kinds, alias_kinds = defaultdict(set), defaultdict(set)
    for raw_name, kind in conn.execute(select(names.c.raw_name, names.c.kind).join(credits)
            .where(credits.c.role == "publisher").distinct()):
        publisher_kinds[raw_name].add(kind)
    for raw_name, kind in conn.execute(select(names.c.raw_name, names.c.kind).join(aliases)
            .join(roles, roles.c.entity_id == aliases.c.entity_id)
            .where(roles.c.role == "publisher").distinct()):
        alias_kinds[raw_name].add(kind)

    def entity_member(entity_id):
        row = entity_rows.get(entity_id)
        if row is None:
            raise CatalogNotFound("entity not found")
        visited = set()
        while row["merged_into_id"] is not None:
            if row["entity_id"] in visited:
                raise ValueError("legacy identity merge cycle")
            visited.add(row["entity_id"])
            row = entity_rows.get(row["merged_into_id"])
            if row is None:
                raise CatalogNotFound("entity not found")
        return {"entity_id": row["entity_id"], "name_id": None,
                "reviewed_revision": row["revision"], "snapshot": {"kind": row["kind"], "display_name": row["display_name"]}}

    def name_member(raw_name, kind):
        key = (kind, raw_name)
        if key not in name_rows:
            name_rows[key] = identifier("names", "name_id")
            pending_names.append({"name_id": name_rows[key], "kind": kind, "raw_name": raw_name})
        name_id = name_rows[key]
        return {"entity_id": None, "name_id": name_id, "reviewed_revision": None,
                "snapshot": {"kind": kind, "raw_name": raw_name}}

    def publisher_member(key):
        if key.startswith("canonical:") and key[10:].isdecimal():
            return entity_member(int(key[10:]))
        if key.startswith("raw:") and key[4:]:
            kinds = publisher_kinds[key[4:]] or alias_kinds[key[4:]]
            if len(kinds) > 1:
                raise ValueError("ambiguous publisher suggestion name type; review before import")
            return name_member(key[4:], next(iter(kinds)) if kinds else "organization")
        raise ValueError("unsupported legacy publisher review identity")

    def proposal(members, name, evidence, status):
        members = {catalog._member_key(member): member for member in members}
        proposal_id = identifier("proposals", "proposal_id")
        pending_proposals.append({"proposal_id": proposal_id, "kind": "identity", "display_name": name,
            "status": status, "evidence": json.loads(json.dumps(evidence, default=str))})
        pending_members.extend({"proposal_id": proposal_id, **member} for member in members.values())

    for row in source.get("normalization_suggestions", []):
        if row.get("status") != "open":
            continue
        kind = "person" if row["entity_type"] == "personality" else "organization"
        members = [name_member(row["raw_name"], kind)]
        if row.get("target_canonical_id") is not None:
            members.append(entity_member(row["target_canonical_id"]))
        proposal(members, row["normalized_name"], {"source": "legacy.normalization_suggestions",
            "legacy_id": row["suggestion_id"], "legacy_status": row["status"], "original": row}, "pending")
    for row in source.get("publisher_merge_proposals", []):
        if row["status"] not in {"pending", "staged", "skipped"}:
            continue
        reviewed = row.get("review_edit") or row["proposal"]
        members = [publisher_member(key) for key in reviewed["member_ids"]]
        proposal(members, reviewed.get("display_name") or row["proposal"]["proposed_name"],
            {"source": "legacy.publisher_merge_proposals", "legacy_id": row["proposal_id"],
             "legacy_status": row["status"], "original": row}, "deferred" if row["status"] == "skipped" else "pending")
    _copy_rows(conn, names, pending_names)
    _copy_rows(conn, catalog.table("proposals"), pending_proposals)
    _copy_rows(conn, catalog.table("proposal_members"), pending_members)
    flushed_names = len(pending_names)
    pairs = []
    for row in source.get("publisher_separations", []):
        pairs.append((publisher_member(row["left_key"]), publisher_member(row["right_key"])))
    _copy_rows(conn, names, pending_names[flushed_names:])
    for pair in pairs:
        keys = sorted({catalog._member_key(member) for member in pair})
        for left, right in combinations(keys, 2):
            conn.execute(insert(catalog.table("separations")).values(left_key=left, right_key=right, actor=actor).on_conflict_do_nothing())
