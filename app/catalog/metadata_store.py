"""Normalized bibliographic reader and shared comparison helpers."""

from sqlalchemy import and_, select

from app.catalog.metadata import SCALARS



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

    def _metadata(self, conn, md5, *, lock=True):
        def record(kind, key):
            if lock:
                return self._record(conn, kind, key)
            table = self.table("documents" if kind == "document" else "publications")
            column = table.c.md5 if kind == "document" else table.c.publication_id
            return dict(conn.execute(select(table).where(column == key)).mappings().one())

        doc = record("document", md5)
        pub = record("publication", doc["publication_id"])
        publication_id = pub["publication_id"]

        def rows(name):
            table = self.table(name)
            return [dict(row) for row in conn.execute(select(table).where(table.c.publication_id == publication_id).order_by(table.c.position)).mappings()]

        credits, names, entities = self.table("contributions"), self.table("names"), self.table("entities")
        groups = self.table("credit_groups")
        credit_rows = conn.execute(select(credits, groups.c.role_name, names.c.kind, names.c.raw_name, entities.c.display_name)
            .join(groups, and_(groups.c.publication_id == credits.c.publication_id,
                              groups.c.role == credits.c.role, groups.c.position == credits.c.position))
            .join(names, names.c.name_id == credits.c.name_id)
            .outerjoin(entities, and_(entities.c.entity_id == credits.c.entity_id,
                                     entities.c.status == "active", entities.c.approval == "confirmed",
                                     credits.c.resolution == "confirmed")).where(
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
        languages = self.table("publication_languages")
        access = self.table("document_access_modes")
        modes, mode_items = self.table("sufficient_modes"), self.table("sufficient_mode_items")
        sufficient = []
        for position in conn.execute(select(modes.c.position).where(modes.c.md5 == md5).order_by(modes.c.position)).scalars():
            sufficient.append(list(conn.execute(select(mode_items.c.mode).where(
                mode_items.c.md5 == md5, mode_items.c.group_position == position,
            ).order_by(mode_items.c.position)).scalars()))
        refs = self.table("references")
        ref = conn.execute(select(refs).where(refs.c.publication_id == publication_id)).mappings().first()
        based_on = None
        if ref is not None:
            based_on = {key: ref[column] for key, column in {"@type": "work_type", "name": "name", "inLanguage": "language"}.items() if ref[column] is not None}
            urls = self.table("reference_urls")
            if ref["urls_present"]:
                based_on["url"] = list(conn.execute(select(urls.c.url).where(
                    urls.c.publication_id == publication_id).order_by(urls.c.position)).scalars())
            authors = self.table("reference_authors")
            values = [{"@type": row["kind"], "name": row["name"]} for row in conn.execute(select(authors).where(authors.c.publication_id == publication_id).order_by(authors.c.position)).mappings()]
            if values:
                based_on["author"] = values
        return {
            "publication_id": publication_id, "revision": pub["revision"],
            "scalars": {column: pub[column] for column in SCALARS.values() if pub[column] is not None},
            "languages": list(conn.execute(select(languages.c.language).where(
                languages.c.publication_id == publication_id).order_by(languages.c.position)).scalars()),
            "credits": [dict(row) for row in credit_rows],
            "identifiers": [row["value"] for row in rows("identifiers") if row["kind"] == "isbn"],
            "genres": [row["value"] for row in rows("genres")], "subjects": subjects,
            "audiences": audiences, "audience_array": pub["audience_array"],
            "access_modes": list(conn.execute(select(access.c.mode).where(access.c.md5 == md5).order_by(access.c.position)).scalars()),
            "sufficient_modes": sufficient,
            "based_on": based_on,
        }
