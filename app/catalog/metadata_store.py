"""Bibliographic writes, projections, evidence, and protected-field proposals."""

import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.catalog.metadata import SCALARS, compose_metadata, decompose_metadata, items
from app.catalog.contracts import CatalogConflict, integer, nonblank


RELATIONAL_FIELDS = {"credits", "identifiers", "genres", "subjects", "audiences", "based_on"}


def unmanaged_subjects(subjects):
    def managed(term):
        termset = term.get("inDefinedTermSet")
        return isinstance(termset, dict) and termset.get("name", "").casefold() in {"ddc", "categorypath"}
    return [term for term in subjects if not managed(term)]


def comparable_credits(credits):
    return [{key: row[key] for key in ("role", "role_name", "position", "nested_position", "raw_name", "kind")}
            for row in credits]


def credit_signature(credit):
    return tuple(credit[key] for key in ("role", "role_name", "position", "nested_position", "raw_name", "kind"))


class MetadataStore:
    def decide_metadata(self, proposal_id, *, revision, publication_revision, decision, actor, document_revision=None):
        if not isinstance(decision, str) or decision not in {"apply", "reject", "defer"}:
            raise ValueError("unsupported metadata decision")
        with self.engine.begin() as conn:
            proposal = self._record(conn, "proposal", proposal_id, revision=revision)
            if proposal["kind"] != "metadata" or proposal["status"] not in {"pending", "deferred"}:
                raise CatalogConflict("proposal is no longer open")
            publication = self._record(conn, "publication", proposal["publication_id"], revision=publication_revision)
            if decision == "apply":
                md5 = proposal["evidence"]["md5"]
                if proposal["field_changes"].keys() & {"access_modes", "sufficient_modes"}:
                    self._record(conn, "document", md5, revision=integer(document_revision, "document_revision"))
                current = self._metadata(conn, md5)
                if current["publication_id"] != publication["publication_id"]:
                    raise CatalogConflict("document publication changed; review a new proposal")
                for field, value in proposal["field_changes"].items():
                    if field in SCALARS.values():
                        current["scalars"][field] = value
                    elif field in RELATIONAL_FIELDS | {"languages", "access_modes", "sufficient_modes", "audience_array"}:
                        current[field] = value
                    elif field in {"inclusion", "evaluation_method", "classification_id", "collection_id"}:
                        continue
                    else:
                        raise ValueError("unsupported proposed metadata field")
                self._apply_metadata(conn, md5, current, compose_metadata(current), actor, False, publication_revision)
                changes = {field: value for field, value in proposal["field_changes"].items()
                           if field in {"inclusion", "evaluation_method", "classification_id", "collection_id"}}
                if changes:
                    publication = self._record(conn, "publication", publication["publication_id"])
                    self._protect(conn, "publication", publication["publication_id"], changes, actor)
                    self._update(conn, "publication", publication["publication_id"], publication, changes, actor)
            return self._update(conn, "proposal", proposal_id, proposal,
                {"status": {"apply": "applied", "reject": "rejected", "defer": "deferred"}[decision]}, actor)

    def _name(self, conn, kind, raw_name):
        table = self.table("names")
        statement = insert(table).values(kind=kind, raw_name=raw_name)
        return conn.execute(statement.on_conflict_do_update(
            index_elements=[table.c.kind, table.c.raw_name], set_={"raw_name": raw_name},
        ).returning(table.c.name_id)).scalar_one()

    def _metadata(self, conn, md5):
        doc = self._record(conn, "document", md5)
        pub = self._record(conn, "publication", doc["publication_id"])
        publication_id = pub["publication_id"]

        def rows(name):
            table = self.table(name)
            return [dict(row) for row in conn.execute(select(table).where(table.c.publication_id == publication_id).order_by(table.c.position)).mappings()]

        credits, names, entities = self.table("contributions"), self.table("names"), self.table("entities")
        credit_rows = conn.execute(select(credits, names.c.kind, names.c.raw_name, entities.c.display_name).join(names).outerjoin(entities).where(
            credits.c.publication_id == publication_id,
        ).order_by(credits.c.role, credits.c.position, credits.c.nested_position)).mappings().all()
        subject_rows = rows("subjects")
        subjects = []
        for row in subject_rows:
            term = {"@type": "DefinedTerm"}
            if row["name"] is not None:
                term["name"] = row["name"]
            if row["term_code"] is not None:
                term["termCode"] = row["term_code"]
            if row["set_is_url"]:
                term["inDefinedTermSet"] = row["set_url"]
            else:
                term["inDefinedTermSet"] = {"@type": "DefinedTermSet", "name": row["set_name"]}
                if row["set_url"] is not None:
                    term["inDefinedTermSet"]["url"] = row["set_url"]
            subjects.append(term)
        audiences = []
        for row in rows("audiences"):
            audience = {"@type": row["kind"]}
            for field, column in {"audienceType": "audience_type", "suggestedMinAge": "min_age", "suggestedMaxAge": "max_age"}.items():
                if row[column] is not None:
                    audience[field] = row[column]
            audiences.append(audience)
        modes = self.table("sufficient_modes")
        refs = self.table("references")
        ref = conn.execute(select(refs).where(refs.c.publication_id == publication_id)).mappings().first()
        based_on = None
        if ref is not None:
            based_on = {key: ref[column] for key, column in {"@type": "work_type", "name": "name", "inLanguage": "language", "url": "urls"}.items() if ref[column] is not None}
            authors = self.table("reference_authors")
            values = [{"@type": row["kind"], "name": row["name"]} for row in conn.execute(select(authors).where(authors.c.publication_id == publication_id).order_by(authors.c.position)).mappings()]
            if values:
                based_on["author"] = values
        return {
            "publication_id": publication_id, "revision": pub["revision"],
            "scalars": {column: pub[column] for column in SCALARS.values() if pub[column] is not None},
            "languages": pub["languages"], "credits": [dict(row) for row in credit_rows],
            "identifiers": [row["value"] for row in rows("identifiers")],
            "genres": [row["value"] for row in rows("genres")], "subjects": subjects,
            "audiences": audiences, "audience_array": pub["audience_array"],
            "access_modes": doc["access_modes"],
            "sufficient_modes": list(conn.execute(select(modes.c.modes).where(modes.c.md5 == md5).order_by(modes.c.position)).scalars()),
            "based_on": based_on,
        }

    def metadata(self, md5):
        with self.engine.begin() as conn:
            return self._metadata(conn, md5)

    def schema_org(self, md5):
        with self.engine.begin() as conn:
            record = self._metadata(conn, md5)
            for credit in record["credits"]:
                if credit.get("display_name") is not None:
                    credit["raw_name"] = credit["display_name"]
            pub = self._record(conn, "publication", record["publication_id"])
            if pub["classification_id"] is not None:
                classifications = self.table("classifications")
                node_id = conn.execute(select(classifications.c.node_id).where(
                    classifications.c.classification_id == pub["classification_id"])).scalar_one()
                path = self._classification_path(conn, node_id)
                record["subjects"] = unmanaged_subjects(record["subjects"]) + [
                    {"@type": "DefinedTerm", "termCode": value,
                     "inDefinedTermSet": {"@type": "DefinedTermSet", "name": termset}}
                    for termset, value in (("DDC", path["ddc"]), ("CategoryPath", " > ".join(path["path_en"])))]
            return compose_metadata(record)

    def _replace_relations(self, conn, publication_id, fields, record):
        for field in fields:
            if field == "credits":
                table = self.table("contributions")
                names = self.table("names")
                old = {credit_signature(row): row for row in conn.execute(select(table, names.c.kind, names.c.raw_name)
                    .join(names).where(table.c.publication_id == publication_id)).mappings()}
                retained = [old[credit_signature(credit)]["contribution_id"] for credit in record[field] if credit_signature(credit) in old]
                conn.execute(table.delete().where(table.c.publication_id == publication_id, ~table.c.contribution_id.in_(retained)))
                for credit in record[field]:
                    if credit_signature(credit) in old:
                        continue
                    values = {key: credit[key] for key in ("role", "role_name", "position", "nested_position")}
                    name_id = self._name(conn, credit["kind"], credit["raw_name"])
                    conn.execute(table.insert().values(publication_id=publication_id, name_id=name_id, **values))
            elif field == "based_on":
                for name in ("references", "reference_authors"):
                    table = self.table(name)
                    conn.execute(table.delete().where(table.c.publication_id == publication_id))
                source = record[field]
                if source is not None:
                    if set(source) - {"@type", "name", "inLanguage", "url", "author"}:
                        raise ValueError("unsupported source-work reference")
                    conn.execute(self.table("references").insert().values(publication_id=publication_id,
                        work_type=source.get("@type"), name=source.get("name"), language=source.get("inLanguage"), urls=source.get("url")))
                    for position, entity in enumerate(items(source.get("author"))):
                        conn.execute(self.table("reference_authors").insert().values(publication_id=publication_id,
                            kind=entity["@type"], name=entity["name"], position=position))
            else:
                table = self.table(field)
                conn.execute(table.delete().where(table.c.publication_id == publication_id))
                for position, item in enumerate(record[field]):
                    values = {"publication_id": publication_id, "position": position}
                    if field == "identifiers":
                        values.update(kind="isbn", value=item, normalized=re.sub(r"[^0-9X]", "", item.upper()))
                    elif field == "genres":
                        values["value"] = item
                    elif field == "subjects":
                        termset = item["inDefinedTermSet"]
                        values.update(name=item.get("name"), term_code=item.get("termCode"),
                            set_is_url=isinstance(termset, str), set_name=termset.get("name") if isinstance(termset, dict) else None,
                            set_url=termset if isinstance(termset, str) else termset.get("url"))
                    elif field == "audiences":
                        values.update(kind=item["@type"], audience_type=item.get("audienceType"),
                                      min_age=item.get("suggestedMinAge"), max_age=item.get("suggestedMaxAge"))
                    conn.execute(table.insert().values(**values))

    def apply_metadata(self, md5, schema_org, *, actor, automated=False, revision=None, document_revision=None):
        incoming = decompose_metadata(schema_org)
        with self.engine.begin() as conn:
            if document_revision is not None:
                self._record(conn, "document", md5, revision=document_revision)
            return self._apply_metadata(conn, md5, incoming, schema_org, actor, automated, revision)

    def _apply_metadata(self, conn, md5, incoming, source, actor, automated, revision):
        doc = self._record(conn, "document", md5)
        key = doc["publication_id"]
        pub = self._record(conn, "publication", key, revision=revision)
        before = self._metadata(conn, md5)
        # A metadata editor starts from the canonical projection. Matching names
        # keep the original observation and its explicitly resolved identity.
        prior = {(row["role"], row["position"], row["nested_position"]): row for row in before["credits"]}
        for credit in incoming["credits"]:
            current = prior.get((credit["role"], credit["position"], credit["nested_position"]))
            if current and credit["kind"] == current["kind"] and credit["role_name"] == current["role_name"]:
                if credit["raw_name"] == current.get("display_name"):
                    credit["raw_name"] = current["raw_name"]
        if pub["classification_id"] is not None:
            incoming["subjects"] = unmanaged_subjects(incoming["subjects"])
        protected = self._protected(conn, "publication", key) if automated else set()
        changed = {field for field in RELATIONAL_FIELDS - {"credits"} if incoming[field] != before[field]}
        if comparable_credits(incoming["credits"]) != comparable_credits(before["credits"]):
            changed.add("credits")
        scalars = {field: value for field, value in incoming["scalars"].items() if pub[field] != value}
        if pub["languages"] != incoming["languages"]:
            scalars["languages"] = incoming["languages"]
        blocked = {field: scalars[field] for field in scalars.keys() & protected}
        blocked.update({field: incoming[field] for field in changed & protected})
        if "audiences" in blocked:
            blocked["audience_array"] = incoming["audience_array"]
        doc_protected = self._protected(conn, "document", md5) if automated else set()
        file_changes = {field: incoming[field] for field in {"access_modes", "sufficient_modes"} if incoming[field] != before[field]}
        blocked.update({field: value for field, value in file_changes.items() if field in doc_protected})
        if blocked:
            conn.execute(self.table("proposals").insert().values(kind="metadata", publication_id=key,
                evidence={"actor": actor, "md5": md5, "publication_revision": pub["revision"]}, field_changes=blocked))
        values = {field: value for field, value in scalars.items() if field not in protected}
        values["audience_array"] = incoming["audience_array"] if "audiences" not in protected else pub["audience_array"]
        self._replace_relations(conn, key, changed - protected, incoming)
        if not automated:
            self._protect(conn, "publication", key, set(scalars) | changed, actor)
        if "sufficient_modes" in file_changes and "sufficient_modes" not in doc_protected:
            modes = self.table("sufficient_modes")
            conn.execute(modes.delete().where(modes.c.md5 == md5))
            for position, item in enumerate(incoming["sufficient_modes"]):
                conn.execute(modes.insert().values(md5=md5, position=position, modes=item))
        if file_changes.keys() - doc_protected:
            self._update(conn, "document", md5, doc,
                {"access_modes": incoming["access_modes"]} if "access_modes" in file_changes and "access_modes" not in doc_protected else {}, actor)
        if not automated:
            self._protect(conn, "document", md5, {"access_modes", "sufficient_modes"}, actor)
        conn.execute(self.table("evidence").insert().values(md5=md5, record_kind="publication", record_key=str(key), source=actor, payload=source))
        self._update(conn, "publication", key, pub, values, actor)
        after = self._metadata(conn, md5)
        self._audit(conn, "metadata", key, before, after, actor)
        return after

    def evidence(self, md5, *, after_id=0, limit=25):
        integer(after_id, "after_id", minimum=0)
        integer(limit, "limit")
        if limit > 100:
            raise ValueError("limit must not exceed 100")
        table = self.table("evidence")
        with self.engine.begin() as conn:
            self._record(conn, "document", md5)
            rows = [dict(row) for row in conn.execute(select(table).where(table.c.md5 == md5,
                table.c.evidence_id > after_id).order_by(table.c.evidence_id).limit(limit)).mappings()]
            return {"items": rows, "next_cursor": rows[-1]["evidence_id"] if rows else after_id}

    def set_location(self, md5, *, provider, purpose, actor, **values):
        nonblank(provider, "provider")
        nonblank(purpose, "purpose")
        allowed = {"locator", "source_path", "resource_id", "public_url", "public_key", "size", "etag", "verified_at"}
        if set(values) - allowed:
            raise ValueError("unsupported storage location fields")
        if values.get("size") is not None:
            integer(values["size"], "size", minimum=0)
        for field in allowed - {"size", "verified_at"}:
            if values.get(field) is not None and not isinstance(values[field], str):
                raise ValueError(f"{field} must be text or null")
        with self.engine.begin() as conn:
            doc = self._record(conn, "document", md5)
            if doc["restricted"] and provider == "yandex":
                values.update(public_url=None, public_key=None)
            table = self.table("locations")
            before = conn.execute(select(table).where(table.c.md5 == md5, table.c.provider == provider, table.c.purpose == purpose).with_for_update()).mappings().first()
            statement = insert(table).values(md5=md5, provider=provider, purpose=purpose, **values)
            changes = {**values, "revision": table.c.revision + 1}
            row = dict(conn.execute(statement.on_conflict_do_update(
                index_elements=[table.c.md5, table.c.provider, table.c.purpose], set_=changes,
            ).returning(table)).mappings().one())
            self._audit(conn, "location", row["location_id"], dict(before) if before else None, row, actor)
            return row
