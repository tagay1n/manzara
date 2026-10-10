
from app.runtime_config import config_integer
from app.settings import configured_schema

"""Read-only normalized catalog snapshot for the static Library bundle."""

import re
from collections import defaultdict

from sqlalchemy import text

from app.catalog.metadata import SCALARS, compose_metadata
from app.modules.library.site_export import ExportStopped
from app.postgres_engine import acquire_postgres_engine, release_postgres_engine

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COHORT = """
    WITH export_publications AS (
        SELECT * FROM {catalog}catalog_publications
        WHERE inclusion='included' AND merged_into_id IS NULL
    ), export_documents AS (
        SELECT d.* FROM {catalog}catalog_documents d
        JOIN export_publications p USING(publication_id)
    )
"""
_REQUIRED = {
    "catalog_documents": {"md5", "publication_id", "mime_type", "complete", "restricted", "selected"},
    "catalog_publications": {"publication_id", "inclusion", "merged_into_id", "has_metadata", "metadata_present",
                             "classification_id", "collection_id", "audience_array", *SCALARS.values()},
    "catalog_locations": {"md5", "provider", "purpose", "locator", "size", "verified_at"},
    "catalog_collections": {"collection_id", "title", "include_in_library"},
    "catalog_classifications": {"classification_id", "node_id"},
    "catalog_classification_nodes": {"node_id", "parent_id", "ddc", "label_en", "label_tt"},
    "catalog_entities": {"entity_id", "kind", "display_name", "approval", "status"},
    "catalog_aliases": {"name_id", "entity_id", "approval"},
    "catalog_names": {"name_id", "kind", "raw_name"},
    "catalog_credit_groups": {"publication_id", "role", "position", "role_name"},
    "catalog_contributions": {"publication_id", "role", "position", "nested_position", "name_id", "entity_id", "resolution"},
    "catalog_publication_languages": {"publication_id", "position", "language"},
    "catalog_identifiers": {"publication_id", "kind", "position", "value"},
    "catalog_genres": {"publication_id", "position", "value"},
    "catalog_subjects": {"publication_id", "position", "name", "term_code", "set_name", "set_url", "set_is_url"},
    "catalog_audiences": {"publication_id", "position", "kind", "audience_type", "min_age", "max_age"},
    "catalog_document_access_modes": {"md5", "position", "mode"},
    "catalog_sufficient_modes": {"md5", "position"},
    "catalog_sufficient_mode_items": {"md5", "group_position", "position", "mode"},
    "catalog_references": {"publication_id", "work_type", "name", "language", "urls_present"},
    "catalog_reference_urls": {"publication_id", "position", "url"},
    "catalog_reference_authors": {"publication_id", "position", "kind", "name"},
    "catalog_preview_requests": {"request_id", "md5", "status", "private", "source_page_count"},
    "catalog_preview_pages": {"request_id", "role", "page_number", "small_key", "large_key"},
    "document_cleanup_queue": {"md5", "scope", "status"},
}
_CANDIDATES = """
    SELECT d.md5,d.publication_id,d.mime_type,d.complete,d.restricted,d.selected,
           p.has_metadata,p.metadata_present,p.work_type,p.name,p.description,p.edition,
           p.date_published,p.page_count,p.audience_array,p.classification_id,p.collection_id,
           s.locator AS document_url,s.size AS primary_storage_size,
           s.verified_at AS primary_storage_verified_at,
           content.locator AS content_url,content.verified_at AS content_verified_at,
           collection.title AS collection_title,collection.include_in_library AS collection_include,
           EXISTS(SELECT 1 FROM {catalog}document_cleanup_queue q WHERE q.md5=d.md5
                  AND q.scope='document' AND q.status IN ('planned','running','failed')) AS has_active_cleanup
    FROM export_documents d JOIN export_publications p USING(publication_id)
    LEFT JOIN {catalog}catalog_locations s ON s.md5=d.md5 AND s.provider='s3' AND s.purpose='primary'
    LEFT JOIN {catalog}catalog_locations content ON content.md5=d.md5 AND content.provider='s3' AND content.purpose='content'
    LEFT JOIN {catalog}catalog_collections collection ON collection.collection_id=p.collection_id
    ORDER BY d.md5
"""
_CREDITS = """
    SELECT c.publication_id,c.role,c.position,c.nested_position,g.role_name,n.raw_name,
           CASE WHEN c.resolution='confirmed' AND e.approval='confirmed' AND e.status='active'
                THEN e.entity_id END AS entity_id,
           CASE WHEN c.resolution='confirmed' AND e.approval='confirmed' AND e.status='active'
                THEN e.display_name ELSE n.raw_name END AS display_name,
           CASE WHEN c.resolution='confirmed' AND e.approval='confirmed' AND e.status='active'
                THEN e.kind ELSE n.kind END AS kind
    FROM {catalog}catalog_contributions c JOIN export_publications p USING(publication_id)
    JOIN {catalog}catalog_credit_groups g USING(publication_id,role,position)
    JOIN {catalog}catalog_names n USING(name_id)
    LEFT JOIN {catalog}catalog_entities e USING(entity_id)
    ORDER BY c.publication_id,c.role,c.position,c.nested_position
"""
_ENTITIES = """
    SELECT DISTINCT e.entity_id,e.kind,e.display_name,
           CASE c.role WHEN 'publisher' THEN 'publisher' ELSE 'personality' END AS entity_type,
           coalesce(n.raw_name,e.display_name) AS alias_name
    FROM {catalog}catalog_contributions c JOIN export_publications p USING(publication_id)
    JOIN {catalog}catalog_entities e USING(entity_id)
    LEFT JOIN {catalog}catalog_aliases a ON a.entity_id=e.entity_id AND a.approval='confirmed'
    LEFT JOIN {catalog}catalog_names n ON n.name_id=a.name_id
    WHERE c.resolution='confirmed' AND e.approval='confirmed' AND e.status='active'
    ORDER BY entity_type,e.entity_id,alias_name
"""


def _present(values):
    return {key: value for key, value in values.items() if value is not None}


def _subject(row):
    termset = row["set_url"] if row["set_is_url"] else _present({
        "@type": "DefinedTermSet", "name": row["set_name"], "url": row["set_url"],
    })
    return _present({"@type": "DefinedTerm", "name": row["name"], "termCode": row["term_code"],
                     "inDefinedTermSet": termset})


def _managed_subject(term):
    termset = term.get("inDefinedTermSet")
    return isinstance(termset, dict) and str(termset.get("name") or "").lower() in {"ddc", "categorypath"}


class _SnapshotReader:
    def __init__(self, conn, schema, should_stop):
        self.conn, self.prefix, self.should_stop = conn, f'"{schema}".', should_stop

    def rows(self, query):
        if self.should_stop():
            raise ExportStopped("Static Library export stopped during snapshot")
        query = (_COHORT + query).replace("{catalog}", self.prefix)
        with self.conn.execute(text(query), execution_options={"yield_per": config_integer("postgres", "read_batch_size")}) as result:
            for index, row in enumerate(result.mappings()):
                if index % config_integer("postgres", "read_batch_size") == 0 and self.should_stop():
                    raise ExportStopped("Static Library export stopped during snapshot")
                yield dict(row)

    def grouped(self, query, key, value):
        result = defaultdict(list)
        for row in self.rows(query):
            result[row[key]].append(value(row))
        return result

    def publication_rows(self, table, *, order="position"):
        # Table/order names are code-owned; only the validated schema is interpolated.
        return f"""SELECT r.* FROM {{catalog}}catalog_{table} r
            JOIN export_publications p USING(publication_id) ORDER BY r.publication_id,r.{order}"""

    def publication_list(self, table, value, *, order="position"):
        return self.grouped(self.publication_rows(table, order=order), "publication_id", value)

    def references(self):
        urls = self.publication_list("reference_urls", lambda row: row["url"])
        authors = self.publication_list("reference_authors", lambda row: _present({"@type": row["kind"], "name": row["name"]}))
        references = {}
        for row in self.rows(self.publication_rows("references", order="publication_id")):
            key = row["publication_id"]
            reference = _present({"@type": row["work_type"], "name": row["name"], "inLanguage": row["language"]})
            if row["urls_present"]:
                reference["url"] = urls[key]
            if authors[key]:
                reference["author"] = authors[key]
            references[key] = reference
        return references

    def sufficient_modes(self):
        mode_items = defaultdict(list)
        for row in self.rows("""
            SELECT m.* FROM {catalog}catalog_sufficient_mode_items m
            JOIN export_documents d USING(md5) ORDER BY m.md5,m.group_position,m.position
        """):
            mode_items[(row["md5"], row["group_position"])].append(row["mode"])
        return self.grouped("""
            SELECT m.md5,m.position FROM {catalog}catalog_sufficient_modes m
            JOIN export_documents d USING(md5) ORDER BY m.md5,m.position
        """, "md5", lambda row: mode_items[(row["md5"], row["position"])])

    def previews(self):
        previews = {row["md5"]: {**row, "pages": []} for row in self.rows("""
            SELECT DISTINCT ON(r.md5) r.md5,r.request_id,r.source_page_count,r.private
            FROM {catalog}catalog_preview_requests r JOIN export_documents d USING(md5)
            WHERE r.status='ready' AND r.private IS FALSE ORDER BY r.md5,r.request_id DESC
        """)}
        by_request = {row["request_id"]: row for row in previews.values()}
        for row in self.rows("""
            SELECT page.* FROM {catalog}catalog_preview_pages page
            JOIN {catalog}catalog_preview_requests r USING(request_id) JOIN export_documents d USING(md5)
            WHERE r.status='ready' AND r.private IS FALSE ORDER BY page.request_id,page.page_number
        """):
            if row["request_id"] in by_request:
                by_request[row["request_id"]]["pages"].append(row)
        return previews

    def bibliographic_lists(self):
        return {
            "languages": self.publication_list("publication_languages", lambda row: row["language"]),
            "credits": self.grouped(_CREDITS, "publication_id", dict),
            "identifiers": self.publication_list("identifiers", lambda row: row["value"], order="position,r.kind"),
            "genres": self.publication_list("genres", lambda row: row["value"]),
            "subjects": self.publication_list("subjects", _subject),
            "audiences": self.publication_list("audiences", lambda row: _present({
                "@type": row["kind"], "audienceType": row["audience_type"],
                "suggestedMinAge": row["min_age"], "suggestedMaxAge": row["max_age"],
            })),
            "references": self.references(),
            "access_modes": self.grouped("""
                SELECT a.md5,a.mode FROM {catalog}catalog_document_access_modes a
                JOIN export_documents d USING(md5) ORDER BY a.md5,a.position
            """, "md5", lambda row: row["mode"]),
            "sufficient_modes": self.sufficient_modes(),
            "classifications": {row["classification_id"]: row["path"] for row in self.rows("""
                SELECT c.classification_id,{catalog}catalog_path(c.node_id) AS path
                FROM {catalog}catalog_classifications c
                WHERE EXISTS(SELECT 1 FROM export_publications p WHERE p.classification_id=c.classification_id)
            """)},
        }


def _attach_metadata(document, lists, previews):
    key, md5 = document["publication_id"], document["md5"]
    path = lists["classifications"].get(document["classification_id"])
    terms = lists["subjects"][key]
    if path:
        terms = [term for term in terms if not _managed_subject(term)] + [
            {"@type": "DefinedTerm", "termCode": value,
             "inDefinedTermSet": {"@type": "DefinedTermSet", "name": name}}
            for name, value in (("DDC", path["ddc"]), ("CategoryPath", " > ".join(path["path_en"])))
        ]
        document.update(ddc=path["ddc"], path_en=path["path_en"], path_tt=path.get("path_tt"))
    document["schema_org"] = None
    if document["has_metadata"] and document["metadata_present"]:
        document["schema_org"] = compose_metadata({
            "scalars": {column: document[column] for column in SCALARS.values()},
            **{field: lists[field][key] for field in ("languages", "identifiers", "genres", "audiences")},
            "credits": [{**credit, "raw_name": credit["display_name"]} for credit in lists["credits"][key]],
            "subjects": terms, "audience_array": document["audience_array"],
            "access_modes": lists["access_modes"][md5], "sufficient_modes": lists["sufficient_modes"][md5],
            "based_on": lists["references"].get(key),
        })
    document["contributions"] = lists["credits"][key]
    document["preview"] = previews.get(md5)


class LibrarySiteExportRepository:
    """Use one shared pool connection and one consistent, read-only transaction."""

    def __init__(self, database_url: str, *, schema: str) -> None:
        if not _SCHEMA_RE.fullmatch(schema):
            raise ValueError("Invalid catalog schema")
        self.schema = schema
        self._engine = acquire_postgres_engine(database_url, schema=schema)

    def dispose(self) -> None:
        release_postgres_engine(self._engine)

    def _preflight(self, conn):
        present = defaultdict(set)
        for row in conn.execute(text("""
            SELECT table_name,column_name FROM information_schema.columns
            WHERE table_schema=:schema AND table_name=ANY(:relations)
        """), {"schema": self.schema, "relations": list(_REQUIRED)}).mappings():
            present[row["table_name"]].add(row["column_name"])
        missing = [f"{table}.{column}" for table, columns in _REQUIRED.items()
                   for column in sorted(columns - present[table])]
        if missing:
            raise RuntimeError("Static Library export requires migrated catalog columns: " + ", ".join(missing))
        version_schema = configured_schema("migration_version_schema")
        if not _SCHEMA_RE.fullmatch(version_schema):
            raise ValueError("Invalid migration version schema")
        revision = str(conn.execute(text(
            f'SELECT version_num FROM "{version_schema}".alembic_version_manzara'
        )).scalar_one())
        if not re.fullmatch(r"\d{8}_\d{4}", revision) or int(revision.split("_")[1]) < 62:
            raise RuntimeError("Static Library export requires catalog revision 20261008_0062 or later; apply migrations separately")

    def load_snapshot(self, *, should_stop=lambda: False, log=lambda _message: None):
        """Read normalized facts and exact identities without any catalog mutation."""
        with self._engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            with conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                conn.execute(text("SET LOCAL statement_timeout = '60s'"))
                conn.execute(text("SET LOCAL jit = off"))
                self._preflight(conn)
                log("static library export: catalog preflight passed; reading consistent snapshot")
                reader = _SnapshotReader(conn, self.schema, should_stop)
                candidates = list(reader.rows(_CANDIDATES))
                log(f"static library export: snapshot documents={len(candidates)}")
                lists = reader.bibliographic_lists()
                entities = list(reader.rows(_ENTITIES))
                previews = reader.previews()
                log(f"static library export: snapshot credits={sum(map(len, lists['credits'].values()))} public_previews={len(previews)}")
        for document in candidates:
            if should_stop():
                raise ExportStopped("Static Library export stopped during metadata composition")
            _attach_metadata(document, lists, previews)
        return candidates, entities


__all__ = ["LibrarySiteExportRepository"]
