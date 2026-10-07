"""Contextual name resolution and reviewed identity clusters."""

from itertools import combinations

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from app.catalog.contracts import CatalogConflict, integer, nonblank


class IdentityStore:
    def remove_alias(self, alias_id, *, revision, actor):
        with self.engine.begin() as conn:
            alias = self._record(conn, "alias", alias_id, revision=revision)
            entity = self._record(conn, "entity", alias["entity_id"])
            conn.execute(self.table("aliases").delete().where(self.table("aliases").c.alias_id == alias_id))
            self._update(conn, "entity", entity["entity_id"], entity, {}, actor)
            self._audit(conn, "alias", alias_id, alias, None, actor)
            return {"alias_id": alias_id, "removed": True}

    def proposal_detail(self, proposal_id):
        with self.engine.begin() as conn:
            result = self._record(conn, "proposal", proposal_id)
            members = self.table("proposal_members")
            result["members"] = [dict(row) for row in conn.execute(select(members).where(members.c.proposal_id == proposal_id)).mappings()]
            if result["publication_id"] is not None:
                result["current_publication"] = self._record(conn, "publication", result["publication_id"])
                if result["evidence"].get("md5") is not None:
                    result["current_document"] = self._record(conn, "document", result["evidence"]["md5"])
            else:
                keys = [self._member_key(member) for member in result["members"]]
                separations = self.table("separations")
                result["separations"] = [dict(row) for row in conn.execute(select(separations).where(or_(
                    separations.c.left_key.in_(keys), separations.c.right_key.in_(keys)))).mappings()]
            return result
    def add_alias(self, entity_id, raw_name, *, revision, actor):
        with self.engine.begin() as conn:
            entity = self._record(conn, "entity", entity_id, revision=revision)
            name = nonblank(raw_name, "raw_name")
            row = self._retain_alias(conn, entity_id, entity["kind"], name)
            self._update(conn, "entity", entity_id, entity, {}, actor)
            self._audit(conn, "alias", row["alias_id"], None, row, actor)
            return row

    def create_entity(self, kind, display_name, *, actor, approval="unconfirmed"):
        if not isinstance(kind, str) or kind not in {"person", "organization", "unknown"} or not isinstance(approval, str) or approval not in {"confirmed", "unconfirmed"}:
            raise ValueError("unsupported identity kind or approval")
        with self.engine.begin() as conn:
            table = self.table("entities")
            row = dict(conn.execute(table.insert().values(kind=kind, display_name=nonblank(display_name, "display_name"), approval=approval).returning(table)).mappings().one())
            self._audit(conn, "entity", row["entity_id"], None, row, actor)
            return row

    def _retain_alias(self, conn, entity_id, kind, name, *, approval="confirmed"):
        table = self.table("aliases")
        name_id = self._name(conn, kind, name)
        statement = insert(table).values(entity_id=entity_id, name_id=name_id, approval=approval)
        return dict(conn.execute(statement.on_conflict_do_update(
            index_elements=[table.c.name_id, table.c.entity_id], set_={"approval": approval},
        ).returning(table)).mappings().one())

    def list_aliases(self, entity_id):
        aliases, names = self.table("aliases"), self.table("names")
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(aliases, names.c.raw_name, names.c.kind).join(names).where(aliases.c.entity_id == entity_id).order_by(names.c.raw_name)).mappings()]

    def resolve_contribution(self, contribution_id, entity_id, *, revision, actor):
        integer(contribution_id, "contribution_id")
        with self.engine.begin() as conn:
            table = self.table("contributions")
            credit = self._record(conn, "contribution", contribution_id, revision=revision)
            entity = self._record(conn, "entity", entity_id)
            name = conn.execute(select(self.table("names")).where(self.table("names").c.name_id == credit["name_id"])).mappings().one()
            if entity["status"] != "active" or entity["kind"] not in {"unknown", name["kind"]}:
                raise ValueError("credited name and identity have incompatible types")
            self._retain_alias(conn, entity_id, name["kind"], name["raw_name"])
            self._update(conn, "entity", entity_id, entity, {}, actor)
            after = dict(conn.execute(table.update().where(table.c.contribution_id == contribution_id).values(
                entity_id=entity_id, resolution="confirmed", revision=credit["revision"] + 1,
            ).returning(table)).mappings().one())
            self._protect(conn, "publication", credit["publication_id"], {"credits"}, actor)
            publication = self._record(conn, "publication", credit["publication_id"])
            self._update(conn, "publication", credit["publication_id"], publication, {}, actor)
            self._audit(conn, "contribution", contribution_id, dict(credit), after, actor)
            return after

    def reassign_alias(self, alias_id, entity_id, *, revision, contribution_revisions, actor):
        """Reassignment requires the exact affected mentions, never a raw-name sweep."""
        if not isinstance(contribution_revisions, dict):
            raise ValueError("mention revisions must be a mapping")
        for key, expected in contribution_revisions.items():
            integer(key, "contribution_id")
            integer(expected, "revision")
        with self.engine.begin() as conn:
            aliases, credits = self.table("aliases"), self.table("contributions")
            alias = self._record(conn, "alias", alias_id, revision=revision)
            entity = self._record(conn, "entity", entity_id)
            if entity["status"] != "active":
                raise ValueError("target identity is not active")
            name = conn.execute(select(self.table("names")).where(self.table("names").c.name_id == alias["name_id"])).mappings().one()
            if entity["kind"] not in {"unknown", name["kind"]}:
                raise ValueError("alias and target identity have incompatible types")
            affected = conn.execute(select(credits).where(credits.c.name_id == alias["name_id"], credits.c.entity_id == alias["entity_id"]).order_by(credits.c.contribution_id).with_for_update()).mappings().all()
            if {row["contribution_id"]: row["revision"] for row in affected} != contribution_revisions:
                raise CatalogConflict("review every affected credited mention before reassignment")
            for row in affected:
                conn.execute(credits.update().where(credits.c.contribution_id == row["contribution_id"]).values(entity_id=entity_id, revision=row["revision"] + 1))
                self._protect(conn, "publication", row["publication_id"], {"credits"}, actor)
            self._touch_publications(conn, {row["publication_id"] for row in affected}, actor)
            existing = conn.execute(select(aliases).where(aliases.c.name_id == alias["name_id"], aliases.c.entity_id == entity_id)).mappings().first()
            if existing:
                conn.execute(aliases.delete().where(aliases.c.alias_id == alias_id))
                after = dict(existing)
            else:
                after = dict(conn.execute(aliases.update().where(aliases.c.alias_id == alias_id).values(entity_id=entity_id, approval="confirmed", revision=alias["revision"] + 1).returning(aliases)).mappings().one())
            self._audit(conn, "alias", alias_id, dict(alias), after, actor)
            self._update(conn, "entity", entity_id, entity, {}, actor)
            if alias["entity_id"] != entity_id:
                old_entity = self._record(conn, "entity", alias["entity_id"])
                self._update(conn, "entity", old_entity["entity_id"], old_entity, {}, actor)
            return after

    def create_cluster(self, members, *, display_name, evidence, actor):
        if not isinstance(members, list) or not members or len(members) > 200:
            raise ValueError("cluster requires bounded explicit members")
        with self.engine.begin() as conn:
            table = self.table("proposals")
            row = dict(conn.execute(table.insert().values(kind="identity", display_name=nonblank(display_name, "display_name"), evidence=evidence).returning(table)).mappings().one())
            seen = set()
            for member in members:
                if not isinstance(member, dict) or set(member) not in ({"entity_id"}, {"name_id"}):
                    raise ValueError("member must identify an entity or an observed name")
                field, key = next(iter(member.items()))
                integer(key, field)
                if (field, key) in seen:
                    raise ValueError("duplicate cluster member")
                seen.add((field, key))
                if field == "entity_id":
                    current = self._record(conn, "entity", key)
                    if current["status"] != "active":
                        raise ValueError("cluster member is not active")
                else:
                    current = dict(conn.execute(select(self.table("names")).where(self.table("names").c.name_id == key)).mappings().one())
                conn.execute(self.table("proposal_members").insert().values(
                    proposal_id=row["proposal_id"], **member, snapshot={k: current[k] for k in current if k != "updated_at"},
                    reviewed_revision=current.get("revision"),
                ))
            self._audit(conn, "proposal", row["proposal_id"], None, row, actor)
            return row

    def list_proposals(self, *, kind=None, status="pending", page=1, page_size=25):
        integer(page, "page")
        integer(page_size, "page_size")
        if page_size > 100:
            raise ValueError("page_size must not exceed 100")
        table = self.table("proposals")
        statement = select(table).where(table.c.status == status)
        if kind:
            statement = statement.where(table.c.kind == kind)
        from sqlalchemy import func
        with self.engine.connect() as conn:
            total = conn.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
            rows = conn.execute(statement.order_by(table.c.proposal_id).limit(page_size).offset((page - 1) * page_size)).mappings()
            return {"items": [dict(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    def decide_cluster(self, proposal_id, *, revision, decision, actor, display_name=None, selected_members=None):
        if not isinstance(decision, str) or decision not in {"merge", "keep_separate", "defer", "reject"}:
            raise ValueError("unsupported cluster decision")
        with self.engine.begin() as conn:
            conn.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('catalog-identity-review'))")
            proposal = self._record(conn, "proposal", proposal_id, revision=revision)
            if proposal["kind"] != "identity" or proposal["status"] not in {"pending", "deferred"}:
                raise CatalogConflict("proposal is no longer open")
            members = list(conn.execute(select(self.table("proposal_members")).where(self.table("proposal_members").c.proposal_id == proposal_id)).mappings())
            if selected_members is not None:
                if not isinstance(selected_members, list) or any(not isinstance(key, str) for key in selected_members):
                    raise ValueError("selected members must be an array of identity keys")
                chosen = set(selected_members)
                all_keys = {self._member_key(row) for row in members}
                if not chosen or not chosen <= all_keys:
                    raise ValueError("selected members must identify members of this proposal")
                members = [row for row in members if self._member_key(row) in chosen]
            if decision in {"merge", "keep_separate"}:
                for row in members:
                    if row["entity_id"] is not None:
                        current = self._record(conn, "entity", row["entity_id"], revision=row["reviewed_revision"])
                        if current["status"] != "active":
                            raise CatalogConflict("cluster member is no longer active")
            if decision == "keep_separate":
                table = self.table("separations")
                for left, right in combinations(sorted(self._member_key(row) for row in members), 2):
                    conn.execute(insert(table).values(left_key=left, right_key=right, actor=actor).on_conflict_do_nothing())
            elif decision == "merge":
                self._merge_entities(conn, members, display_name or proposal["display_name"], actor)
            status = {"merge": "applied", "keep_separate": "separate", "defer": "deferred", "reject": "rejected"}[decision]
            return self._update(conn, "proposal", proposal_id, proposal, {"status": status}, actor)

    @staticmethod
    def _member_key(row):
        return f"entity:{row['entity_id']}" if row["entity_id"] is not None else f"name:{row['name_id']}"

    def _merge_entities(self, conn, members, display_name, actor):
        kinds = {row["snapshot"]["kind"] for row in members} - {"unknown"}
        if len(kinds) > 1:
            raise ValueError("a person and an organization cannot be merged")
        name = nonblank(display_name, "display_name")
        entity_ids = sorted(row["entity_id"] for row in members if row["entity_id"] is not None)
        table = self.table("entities")
        if entity_ids:
            target_id = entity_ids[0]
            before = self._record(conn, "entity", target_id)
            self._retain_alias(conn, target_id, before["kind"], before["display_name"])
            self._update(conn, "entity", target_id, before, {"display_name": name, "approval": "confirmed"}, actor)
        else:
            target_id = conn.execute(table.insert().values(kind=next(iter(kinds), "unknown"), display_name=name, approval="confirmed").returning(table.c.entity_id)).scalar_one()
        aliases, credits = self.table("aliases"), self.table("contributions")
        affected_publications = set(conn.execute(select(credits.c.publication_id).where(credits.c.entity_id.in_(entity_ids))).scalars())
        for member in members:
            entity_id, name_id = member["entity_id"], member["name_id"]
            if entity_id is not None:
                current = self._record(conn, "entity", entity_id)
                self._retain_alias(conn, target_id, current["kind"], current["display_name"])
                names = conn.execute(select(self.table("names")).join(aliases).where(aliases.c.entity_id == entity_id)).mappings().all()
                for alias in names:
                    self._retain_alias(conn, target_id, alias["kind"], alias["raw_name"])
                if entity_id != target_id:
                    roles = self.table("entity_roles")
                    for role in conn.execute(select(roles.c.role).where(roles.c.entity_id == entity_id)).scalars():
                        conn.execute(insert(roles).values(entity_id=target_id, role=role).on_conflict_do_nothing())
                    conn.execute(credits.update().where(credits.c.entity_id == entity_id).values(entity_id=target_id, revision=credits.c.revision + 1))
                    self._update(conn, "entity", entity_id, current, {"status": "merged", "merged_into_id": target_id}, actor)
            else:
                raw = conn.execute(select(self.table("names")).where(self.table("names").c.name_id == name_id)).mappings().one()
                self._retain_alias(conn, target_id, raw["kind"], raw["raw_name"])
                # A name can be shared by homonyms. Approving an alias never
                # resolves all occurrences; individual mentions require review.
        self._touch_publications(conn, affected_publications, actor)
        # Explicit decisions reconcile obsolete separation endpoints, keeping other pairs.
        separators = self.table("separations")
        keys = [self._member_key(row) for row in members]
        affected = conn.execute(select(separators).where(or_(
            separators.c.left_key.in_(keys), separators.c.right_key.in_(keys)))).mappings().all()
        conn.execute(separators.delete().where(or_(separators.c.left_key.in_(keys), separators.c.right_key.in_(keys))))
        target_key = f"entity:{target_id}"
        for pair in affected:
            left = target_key if pair["left_key"] in keys else pair["left_key"]
            right = target_key if pair["right_key"] in keys else pair["right_key"]
            if left != right:
                left, right = sorted((left, right))
                conn.execute(insert(separators).values(left_key=left, right_key=right, actor=pair["actor"]).on_conflict_do_nothing())

    def _touch_publications(self, conn, publication_ids, actor):
        for publication_id in sorted(publication_ids):
            before = self._record(conn, "publication", publication_id)
            self._update(conn, "publication", publication_id, before, {}, actor)
