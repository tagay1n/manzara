"""Bulk, read-only catalog snapshot for the scheduled Google export."""

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import Engine, text

from app.catalog.metadata import SCALARS, compose_metadata
from app.postgres_engine import configured_timeout_sql
from app.runtime_config import config_integer


def fetch_document_export(engine: Engine, *, schema: str) -> list[dict[str, Any]]:
    """Compose transport metadata without executing legacy views per document."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError("Invalid catalog schema")
    prefix = f'"{schema}".'
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        with conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(configured_timeout_sql("statement_timeout", "query_timeout_seconds")))
            conn.execute(text("SET LOCAL jit = off"))

            def rows(query):
                with conn.execute(text(query.replace("{catalog}", prefix)),
                                  execution_options={"yield_per": config_integer("postgres", "read_batch_size")}) as result:
                    yield from result.mappings()

            documents = [dict(row) for row in rows("""
                SELECT md5, publication_id, mime_type, complete, restricted
                FROM {catalog}catalog_documents
            """)]
            publications = {row["publication_id"]: dict(row) for row in rows("""
                SELECT publication_id, classification_id, has_metadata, metadata_present,
                       work_type, name, description, edition, date_published, page_count, audience_array
                FROM {catalog}catalog_publications
            """)}
            locations = {(row["md5"], row["provider"], row["purpose"]): dict(row) for row in rows("""
                SELECT md5, provider, purpose, locator, source_path, public_url, size
                FROM {catalog}catalog_locations
                WHERE (provider='yandex' AND purpose='source')
                   OR (provider='s3' AND purpose IN ('primary','content'))
            """)}

            def grouped(query, key, value):
                result = defaultdict(list)
                for row in rows(query):
                    result[row[key]].append(value(row))
                return result

            languages = grouped("""
                SELECT publication_id, language FROM {catalog}catalog_publication_languages
                ORDER BY publication_id, position
            """, "publication_id", lambda row: row["language"])
            credits = grouped("""
                SELECT c.publication_id, c.role, c.position, c.nested_position,
                       g.role_name, n.kind, coalesce(e.display_name,n.raw_name) AS raw_name
                FROM {catalog}catalog_contributions c
                JOIN {catalog}catalog_credit_groups g
                  ON g.publication_id=c.publication_id AND g.role=c.role AND g.position=c.position
                JOIN {catalog}catalog_names n USING (name_id)
                LEFT JOIN {catalog}catalog_entities e USING (entity_id)
                ORDER BY c.publication_id, c.role, c.position, c.nested_position
            """, "publication_id", dict)
            identifiers = grouped("""
                SELECT publication_id, value FROM {catalog}catalog_identifiers
                ORDER BY publication_id, position, kind
            """, "publication_id", lambda row: row["value"])
            genres = grouped("""
                SELECT publication_id, value FROM {catalog}catalog_genres
                ORDER BY publication_id, position
            """, "publication_id", lambda row: row["value"])
            subjects = grouped("""
                SELECT * FROM {catalog}catalog_subjects ORDER BY publication_id, position
            """, "publication_id", _subject)
            audiences = grouped("""
                SELECT * FROM {catalog}catalog_audiences ORDER BY publication_id, position
            """, "publication_id", lambda row: _present({
                "@type": row["kind"], "audienceType": row["audience_type"],
                "suggestedMinAge": row["min_age"], "suggestedMaxAge": row["max_age"],
            }))
            access_modes = grouped("""
                SELECT md5, mode FROM {catalog}catalog_document_access_modes ORDER BY md5, position
            """, "md5", lambda row: row["mode"])
            mode_items = grouped("""
                SELECT md5, group_position, mode FROM {catalog}catalog_sufficient_mode_items
                ORDER BY md5, group_position, position
            """, "md5", dict)
            modes_by_group = defaultdict(list)
            for md5, values in mode_items.items():
                for item in values:
                    modes_by_group[(md5, item["group_position"])].append(item["mode"])
            sufficient_modes = grouped("""
                SELECT md5, position FROM {catalog}catalog_sufficient_modes ORDER BY md5, position
            """, "md5", lambda row: modes_by_group[(row["md5"], row["position"])])
            reference_urls = grouped("""
                SELECT publication_id, url FROM {catalog}catalog_reference_urls
                ORDER BY publication_id, position
            """, "publication_id", lambda row: row["url"])
            reference_authors = grouped("""
                SELECT * FROM {catalog}catalog_reference_authors ORDER BY publication_id, position
            """, "publication_id", lambda row: _present({"@type": row["kind"], "name": row["name"]}))
            references = {}
            for row in rows("SELECT * FROM {catalog}catalog_references"):
                key = row["publication_id"]
                reference = _present({"@type": row["work_type"], "name": row["name"], "inLanguage": row["language"]})
                if row["urls_present"]:
                    reference["url"] = reference_urls[key]
                if reference_authors[key]:
                    reference["author"] = reference_authors[key]
                references[key] = reference
            classifications = {
                row["classification_id"]: row["path"] for row in rows("""
                    SELECT classification_id, {catalog}catalog_path(node_id) AS path
                    FROM {catalog}catalog_classifications
                """)
            }

    records = []
    for document in documents:
        key = document["publication_id"]
        publication = publications[key]
        path = classifications.get(publication["classification_id"])
        terms = subjects[key]
        if path:
            terms = [term for term in terms if not _managed_subject(term)] + [
                {"@type": "DefinedTerm", "termCode": value,
                 "inDefinedTermSet": {"@type": "DefinedTermSet", "name": name}}
                for name, value in (("DDC", path["ddc"]), ("CategoryPath", " > ".join(path["path_en"])))
            ]
        metadata = None
        if publication["has_metadata"] and publication["metadata_present"]:
            metadata = compose_metadata({
                "scalars": {column: publication[column] for column in SCALARS.values()},
                "languages": languages[key], "credits": credits[key],
                "identifiers": identifiers[key], "genres": genres[key], "subjects": terms,
                "audiences": audiences[key], "audience_array": publication["audience_array"],
                "access_modes": access_modes[document["md5"]],
                "sufficient_modes": sufficient_modes[document["md5"]], "based_on": references.get(key),
            })
        yandex = locations.get((document["md5"], "yandex", "source"), {})
        primary = locations.get((document["md5"], "s3", "primary"), {})
        content = locations.get((document["md5"], "s3", "content"), {})
        record = {
            "md5": document["md5"], "mime_type": document["mime_type"],
            "ya_path": yandex.get("source_path"),
            "ya_public_url": yandex.get("public_url") if not document["restricted"] else None,
            "full": document["complete"], "sharing_restricted": document["restricted"],
            "document_url": primary.get("locator"), "content_url": content.get("locator"),
            "size": primary.get("size"),
        }
        record.update(language=",".join(languages[key]) or None, schema_org=metadata)
        records.append(record)
    records.sort(key=lambda row: (row["ya_path"] is None, row["ya_path"] or "", row["md5"]))
    return records


def _present(values):
    return {key: value for key, value in values.items() if value is not None}


def _subject(row):
    termset = row["set_url"] if row["set_is_url"] else _present({
        "@type": "DefinedTermSet", "name": row["set_name"], "url": row["set_url"],
    })
    return _present({"@type": "DefinedTerm", "name": row["name"],
                     "termCode": row["term_code"], "inDefinedTermSet": termset})


def _managed_subject(term):
    termset = term.get("inDefinedTermSet")
    return isinstance(termset, dict) and str(termset.get("name") or "").casefold() in {"ddc", "categorypath"}


def flatten_export_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten the generated envelope, including nested credit groups and roles."""
    source = metadata or {}

    def names(value):
        if isinstance(value, list):
            return [name for item in value for name in names(item)]
        if isinstance(value, dict):
            if value.get("@type") == "Role":
                return names(value.get("contributor"))
            value = value.get("name")
        return [value.strip()] if isinstance(value, str) and value.strip() else []

    def joined(values):
        return ", ".join(dict.fromkeys(values)) or None

    year = re.search(r"(1[5-9]\d{2}|20\d{2})", str(source.get("datePublished") or ""))
    contributors = source.get("contributor") or []
    if isinstance(contributors, dict):
        contributors = [contributors]
    translated = bool(source.get("translator")) or any(
        isinstance(item, dict) and str(item.get("roleName") or "").casefold() == "translator"
        for item in contributors
    )
    return {
        "publisher": joined(names(source.get("publisher"))),
        "author": joined(names(source.get("author"))),
        "title": source.get("name"), "isbn": joined(source.get("isbn") or []),
        "publish_year": int(year.group(1)) if year else None,
        "translated": translated if contributors or source.get("translator") else None,
        "page_count": source.get("numberOfPages"),
    }
