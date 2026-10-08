"""Offline, atomic import of the reviewed corpus into the normalized catalog.

This never reads application configuration or changes the source database. Source
evidence and operational checkpoints are retained; grouping is never inferred.
"""

from collections import defaultdict
import hashlib
import io
import json
import re

from psycopg2 import sql
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app.catalog.metadata import SCALARS, decompose_metadata, items
from app.catalog.metadata_store import unmanaged_subjects
from app.catalog.import_evidence import essential_payload


SOURCE_TABLES = (
    "document", "metadata", "classification", "isbn_keep_many",
    "normalization_canonicals", "normalization_aliases", "normalization_events",
    "normalization_suggestions", "personality_normalization_checkpoints",
    "library_collections", "library_collection_items", "library_collection_events",
    "library_collection_proposals", "library_collection_proposal_items",
    "library_collection_signatures", "library_collection_document_features",
    "library_collection_validation_attempts", "library_book_previews",
    "library_metadata_quality_state", "library_non_pdf_extraction_state",
    "library_upstream_metadata", "library_isbn_duplicate_reviews", "document_cleanup_queue",
    "publisher_merge_analyses", "publisher_merge_proposals", "publisher_review_draft", "publisher_separations",
)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def snapshot_fingerprint(source):
    """PostgreSQL heap order is unrelated to whether the reviewed data changed."""
    ordered = {table: sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str, ensure_ascii=True))
               for table, rows in source.items()}
    return hashlib.sha256(json.dumps(ordered, sort_keys=True, default=str, ensure_ascii=True).encode()).hexdigest()


def read_snapshot(engine, *, domain_schema="monocorpus", dataset_schema="public"):
    for name in (domain_schema, dataset_schema):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("invalid source schema")
    result = {}
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        with conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            version = conn.execute(text("SELECT to_regclass(:relation)"),
                {"relation": f'"{domain_schema}".alembic_version_manzara'}).scalar()
            if version is not None:
                revision = conn.execute(text(f'SELECT version_num FROM "{domain_schema}".alembic_version_manzara')).scalar()
                if revision and str(revision) >= "20261008_0062":
                    raise ValueError("Legacy import is retired for this catalog; restore the recorded PostgreSQL dump instead")
            conn.execute(text("SET LOCAL statement_timeout = '60s'"))
            available = {(row.table_schema, row.table_name) for row in conn.execute(text(
                "SELECT table_schema,table_name FROM information_schema.tables WHERE table_schema IN (:domain,:dataset)"
            ), {"domain": domain_schema, "dataset": dataset_schema})}
            for table in SOURCE_TABLES:
                schema = dataset_schema if table in {"document", "metadata", "classification", "isbn_keep_many"} else domain_schema
                if (schema, table) in available:
                    query = text(f'SELECT * FROM "{schema}"."{table}"').execution_options(yield_per=1024)
                    result[table] = [dict(row) for row in conn.execute(query).mappings()]
    return result


def validate_snapshot(source):
    documents = source.get("document", [])
    ids = [row.get("md5") for row in documents]
    if any(not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key) for key in ids) or len(set(ids)) != len(ids):
        raise ValueError("source document identities are null, duplicate, or invalid")
    metadata_ids = [row["md5"] for row in source.get("metadata", [])]
    if len(metadata_ids) != len(set(metadata_ids)) or set(metadata_ids) - set(ids):
        raise ValueError("source metadata identities are duplicate or orphaned")
    meta = {row["md5"]: row for row in source.get("metadata", [])}
    disagreements, invalid = [], []
    for doc in documents:
        row = meta.get(doc["md5"])
        if row is None or row.get("schema_org") is None:
            continue
        schema_org = _json(row["schema_org"])
        try:
            decompose_metadata(schema_org)
        except (ValueError, KeyError, TypeError, AttributeError):
            invalid.append(doc["md5"])
        if doc.get("language") != schema_org.get("inLanguage"):
            disagreements.append(doc["md5"])
        if row.get("lib") is not None and not isinstance(row["lib"], bool):
            raise ValueError("source library inclusion must be boolean or null")
    if invalid:
        raise ValueError(f"{len(invalid)} source metadata records cannot be represented losslessly; repair before import")
    classification_ids = {row["id"] for row in source.get("classification", [])}
    collection_ids = {row["collection_id"] for row in source.get("library_collections", [])}
    if any(row.get("classification_id") is not None and row["classification_id"] not in classification_ids for row in meta.values()):
        raise ValueError("orphan classification assignment")
    memberships = source.get("library_collection_items", [])
    if len({row["md5"] for row in memberships}) != len(memberships):
        raise ValueError("document has multiple primary collections")
    if any(row["md5"] not in ids or row["collection_id"] not in collection_ids for row in memberships):
        raise ValueError("orphan collection assignment")
    identities = set(ids)
    return {"documents": len(ids), "metadata": len(meta), "language_mismatches": disagreements,
            "unmatched_upstream": sum(row["md5"] not in identities for row in source.get("library_upstream_metadata", []))}


def _confirmed_entities(source):
    """Only explicit, unreverted owner events with matching current names count."""
    current = {row["canonical_id"]: row["display_name"] for row in source.get("normalization_canonicals", [])}
    approved = set()
    for event in source.get("normalization_events", []):
        if event.get("reverted") or event.get("action") not in {"apply_publisher_change_set", "apply_personality_change_set"}:
            continue
        payload = _json(event.get("payload_json") or {})
        for row in payload.get("after", {}).get("canonicals", []):
            key = row["canonical_id"]
            if key in payload.get("touched_canonical_ids", []) and current.get(key) == row.get("display_name"):
                approved.add(key)
        for row in payload.get("renames", []):
            if current.get(row["canonical_id"]) == row.get("display_name"):
                approved.add(row["canonical_id"])
    return approved


def import_snapshot(catalog, source, *, actor, allow_language_mismatches=False, progress=None, evidence_mode="full"):
    if evidence_mode not in {"full", "essential"}:
        raise ValueError("unsupported import evidence mode")
    report = validate_snapshot(source)
    if report["language_mismatches"] and not allow_language_mismatches:
        raise ValueError("language disagreements require explicit review; JSON-LD language will be authoritative")
    fingerprint = snapshot_fingerprint(source)
    with catalog.engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"catalog-import:{catalog.schema}"})
        conn.execute(text("SET LOCAL maintenance_work_mem = '16MB'"))
        conn.execute(text("SET LOCAL gin_pending_list_limit = '256kB'"))
        imports = catalog.table("imports")
        previous = conn.execute(select(imports).where(imports.c.source_fingerprint == fingerprint)).mappings().first()
        if previous is not None:
            if previous['state'] not in {'verified', 'active'}:
                raise ValueError('catalog import is not verified; resume its existing manual loading procedure')
            if previous["manifest"].get("evidence_mode", "full") != evidence_mode:
                raise ValueError("import evidence policy differs; reconcile staging before retrying")
            return previous["manifest"]
        if conn.execute(select(func.count()).select_from(catalog.table("documents"))).scalar_one():
            raise ValueError("target catalog must be empty; this is not a synchronization or dual-write operation")
        if conn.execute(select(func.count()).select_from(catalog.table("publications"))).scalar_one():
            raise ValueError("target catalog publications must be empty")
        manifest = _import(catalog, conn, source, report, actor, progress=progress, evidence_mode=evidence_mode)
        conn.execute(imports.insert().values(source_fingerprint=fingerprint, manifest=manifest, state="verified"))
        for name, column in {
            "publications": "publication_id", "entities": "entity_id", "names": "name_id",
            "collections": "collection_id", "classifications": "classification_id", "classification_nodes": "node_id",
            "preview_requests": "request_id",
            "alias_reviews": "alias_id",
            "proposals": "proposal_id",
        }.items():
            _reset_sequence(catalog, conn, name, column)
        return manifest


def _reset_sequence(catalog, conn, name, column):
    table = catalog.table(name)
    relation = f'"{catalog.schema}"."{table.name}"'
    conn.execute(text("SELECT setval(pg_get_serial_sequence(:relation,:column), COALESCE((SELECT MAX(" + column + ") FROM " + relation + "),1), EXISTS(SELECT 1 FROM " + relation + "))"), {"relation": relation, "column": column})


def _bounded_batches(rows, *, max_rows=256, max_bytes=512 * 1024):
    """Bound query payload peaks; retain a large record as its own batch."""
    batch, size = [], 0
    for row in rows:
        row_size = len(json.dumps(row, default=str).encode("utf-8"))
        if batch and (len(batch) >= max_rows or size + row_size > max_bytes):
            yield batch
            batch, size = [], 0
        batch.append(row)
        size += row_size
    if batch:
        yield batch


def _insert_rows(conn, table, rows):
    _copy_rows(conn, table, rows)


def _copy_rows(conn, table, rows):
    """Stream typed rows in bounded buffers, using the caller's transaction."""
    if not rows:
        return
    columns = tuple(rows[0])
    if any(set(row) != set(columns) for row in rows):
        raise ValueError("COPY rows must have consistent columns")
    command = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT TEXT)").format(
        sql.Identifier(table.schema, table.name), sql.SQL(",").join(map(sql.Identifier, columns)))

    def field(value, column):
        if value is None:
            return r"\N"
        if isinstance(column.type, JSONB):
            value = json.dumps(value, ensure_ascii=False, default=str)
        elif isinstance(column.type, ARRAY):
            def element(item):
                return 'NULL' if item is None else '"' + str(item).replace('\\', '\\\\').replace('"', '\\"') + '"'
            value = '{' + ','.join(element(item) for item in value) + '}'
        elif isinstance(value, bool):
            value = 't' if value else 'f'
        return str(value).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")

    with conn.connection.driver_connection.cursor() as cursor:
        stream = io.StringIO()
        byte_count = row_count = 0
        for row in rows:
            line = "\t".join(field(row[column], table.c[column]) for column in columns) + "\n"
            encoded_bytes = len(line.encode('utf-8'))
            if row_count and (row_count >= 2048 or byte_count + encoded_bytes > 512 * 1024):
                stream.seek(0)
                cursor.copy_expert(command.as_string(cursor), stream)
                stream = io.StringIO()
                byte_count = row_count = 0
            stream.write(line)
            byte_count += encoded_bytes
            row_count += 1
        if row_count:
            stream.seek(0)
            cursor.copy_expert(command.as_string(cursor), stream)


def _copy_evidence(conn, table, rows):
    _copy_rows(conn, table, rows)


_IMPORT_LOADERS = {"evidence": _copy_evidence}


def _import(catalog, conn, source, report, actor, *, progress=None, evidence_mode="full"):
    batch = defaultdict(list)
    meta = {row["md5"]: row for row in source.get("metadata", [])}

    def add(table, **row):
        batch[table].append(row)

    def flush(table, source=None):
        rows = batch[table]
        count = len(rows)
        if progress:
            progress({"stage": "import_table_started", "table": table, "source": source, "rows": count})
        _IMPORT_LOADERS.get(table, _insert_rows)(conn, catalog.table(table), rows)
        batch[table].clear()
        if progress:
            progress({"stage": "import_table_completed", "table": table, "source": source, "rows": count})

    # Lean evidence captures exceptions; checkpoint tables retain their own rows.
    for table, rows in source.items():
        for position, row in enumerate(rows):
            payload = row if evidence_mode == "full" else essential_payload(table, row)
            if evidence_mode == "essential" and table == "document" and "language" in payload:
                bibliographic = _json(meta.get(row["md5"], {}).get("schema_org"))
                language = bibliographic.get("inLanguage") if bibliographic is not None else row.get("language")
                generated = ",".join(part.strip() for part in (language or "").split(",") if part.strip()) or None
                if row.get("language") == generated:
                    payload.pop("language")
            if not payload:
                continue
            add("evidence", md5=row.get("md5"), record_kind="import", record_key=f"{table}:{position}",
                source=f"legacy.{table}", payload=json.loads(json.dumps(payload, default=str)))
        flush("evidence", source=table)
    for row in source.get("library_collections", []):
        add("collections", collection_id=row["collection_id"], title=row["title"], notes=row.get("notes"),
            metadata_template_json=row.get("metadata_template_json", "{}"), applied_at=row.get("applied_at"),
            created_at=row.get("created_at"), normalized_title=row.get("normalized_title"),
            source_updated_at=row.get("updated_at"),
            include_in_library=bool(row.get("include_in_library", True)))
    flush("collections")
    nodes = {}
    translations = {}
    node_rows = {}
    for row in source.get("classification", []):
        parent = None
        path = _json(row["path_en"])
        tt = _json(row.get("path_tt")) or []
        if not isinstance(path, list) or not path or any(not isinstance(label, str) or not label.strip() for label in path):
            raise ValueError("source classification path must be a nonempty text array")
        for position, label in enumerate(path):
            key = (row["ddc"], tuple(part.casefold() for part in path[:position + 1]))
            translated = tt[position] if position < len(tt) else None
            if key not in nodes:
                nodes[key] = len(nodes) + 1
                translations[key] = translated
                add("classification_nodes", node_id=nodes[key], ddc=row["ddc"], parent_id=parent, label_en=label, label_tt=translated)
                node_rows[key] = batch["classification_nodes"][-1]
            elif translated is not None and translations[key] not in {None, translated}:
                raise ValueError("shared classification branch has conflicting translations; review before cutover")
            elif translated is not None and translations[key] is None:
                translations[key] = translated
                node_rows[key]["label_tt"] = translated
            parent = nodes[key]
        add("classifications", classification_id=row["id"], node_id=parent, status=row.get("status", "pending"),
            created_by=row.get("created_by", "gemini"), created_at=row.get("created_at"))
    flush("classification_nodes")
    flush("classifications")
    approved = _confirmed_entities(source)
    entities = {}
    entity_fields = {"surname_full", "surname_initials", "name_full", "name_initials", "father_name_full", "father_name_initials", "title", "sex", "identity_key", "notes"}
    for row in source.get("normalization_canonicals", []):
        key = row["canonical_id"]
        kind = "person" if row["entity_type"] == "personality" else "organization"
        entities[key] = {"kind": kind, "approval": "confirmed" if key in approved else "unconfirmed"}
        add("entities", entity_id=key, display_name=row["display_name"], status=row.get("status", "active"),
            normalized_name=row.get("normalized_name"), created_at=row.get("created_at"),
            source_updated_at=row.get("updated_at"),
            merged_into_id=row.get("merged_into_id"), **entities[key], **{field: row.get(field) for field in entity_fields})
        add("entity_roles", entity_id=key, role="publisher" if row["entity_type"] == "publisher" else "personality")
    # Merged targets need to exist before self-referential references are inserted.
    for row in batch["entities"]:
        target = row.pop("merged_into_id")
        row["_target"] = target
    merged = [(row["entity_id"], row.pop("_target")) for row in batch["entities"]]
    flush("entities")
    for key, target in merged:
        if target is not None:
            conn.execute(catalog.table("entities").update().where(catalog.table("entities").c.entity_id == key).values(merged_into_id=target))
    targets = dict(merged)

    def survivor(key):
        visited = set()
        while targets.get(key) is not None:
            if key in visited:
                raise ValueError("legacy identity merge cycle")
            visited.add(key)
            key = targets[key]
        if key not in entities:
            raise ValueError("orphan legacy identity merge target")
        return key
    flush("entity_roles")
    membership_details = {row["md5"]: row for row in source.get("library_collection_items", [])}
    memberships = {key: row["collection_id"] for key, row in membership_details.items()}
    name_ids = {}
    aliases = {}
    alias_pairs = set()

    def observed(kind, value):
        key = (kind, value)
        if key not in name_ids:
            name_ids[key] = len(name_ids) + 1
            add("names", name_id=name_ids[key], kind=kind, raw_name=value)
        return name_ids[key]

    for row in source.get("normalization_aliases", []):
        kind = entities[row["canonical_id"]]["kind"] if row.get("canonical_id") in entities else (
            "person" if row["entity_type"] == "personality" else "organization")
        if "alias_id" in row:
            columns = catalog.table("alias_reviews").c.keys()
            fields = {key: row[key] for key in columns if key in row and key not in {"name_id", "entity_id"}}
            fields.setdefault("normalized_name", row["raw_name"].casefold())
            fields.setdefault("script_label", "unknown")
            add("alias_reviews", **fields, name_id=observed(kind, row["raw_name"]), entity_id=row.get("canonical_id"))
        if row.get("canonical_id") in entities and row.get("decision_status") == "linked":
            entity_id = survivor(row["canonical_id"])
            identity = entities[entity_id]
            name_id = observed(identity["kind"], row["raw_name"])
            if (name_id, entity_id) not in alias_pairs:
                add("aliases", name_id=name_id, entity_id=entity_id, approval=identity["approval"])
                alias_pairs.add((name_id, entity_id))
            aliases[(identity["kind"], row["raw_name"])] = entity_id
    for publication_id, doc in enumerate(sorted(source.get("document", []), key=lambda row: row["md5"]), 1):
        metadata = meta.get(doc["md5"], {})
        payload = _json(metadata.get("schema_org"))
        record = decompose_metadata(payload) if payload is not None else decompose_metadata({"@type": "CreativeWork"})
        if payload is None:
            record["languages"] = [part.strip() for part in (doc.get("language") or "").split(",") if part.strip()]
        add("publications", publication_id=publication_id, **{field: record["scalars"].get(field) for field in SCALARS.values()}, languages=record["languages"],
            has_metadata=doc["md5"] in meta, metadata_present=payload is not None,
            collection_item_title=membership_details.get(doc["md5"], {}).get("item_title"),
            collection_created_at=membership_details.get(doc["md5"], {}).get("created_at"),
            collection_updated_at=membership_details.get(doc["md5"], {}).get("updated_at"),
            audience_array=record["audience_array"], inclusion={True: "included", False: "excluded", None: "pending"}[metadata.get("lib")],
            evaluation_method=metadata.get("lib_eval_method"), classification_id=metadata.get("classification_id"), collection_id=memberships.get(doc["md5"]))
        add("documents", md5=doc["md5"], publication_id=publication_id, mime_type=doc.get("mime_type"),
            complete=doc.get("full") is True, restricted=doc.get("sharing_restricted") is not False, selected=True,
            content_extraction_method=doc.get("content_extraction_method"), meta_extraction_method=doc.get("meta_extraction_method"), access_modes=record["access_modes"])
        if any(doc.get(field) is not None for field in ("ya_path", "ya_resource_id", "ya_public_url", "ya_public_key")):
            add("locations", md5=doc["md5"], provider="yandex", purpose="source", locator=None,
                source_path=doc.get("ya_path"), resource_id=doc.get("ya_resource_id"), public_url=doc.get("ya_public_url"), public_key=doc.get("ya_public_key"),
                size=None, etag=None, verified_at=None)
        for field, purpose in (("document_url", "primary"), ("content_url", "content")):
            if doc.get(field) is not None or purpose == "primary" and any(doc.get(key) is not None for key in
                    ("primary_storage_size", "primary_storage_etag", "primary_storage_verified_at")):
                add("locations", md5=doc["md5"], provider="s3", purpose=purpose, locator=doc.get(field), source_path=None, resource_id=None,
                    public_url=None, public_key=None, size=doc.get("primary_storage_size") if purpose == "primary" else None,
                    etag=doc.get("primary_storage_etag") if purpose == "primary" else None,
                    verified_at=doc.get("primary_storage_verified_at") if purpose == "primary" else None)
        for credit in record["credits"]:
            name_id = observed(credit["kind"], credit["raw_name"])
            entity_id = aliases.get((credit["kind"], credit["raw_name"]))
            add("contributions", publication_id=publication_id, name_id=name_id, entity_id=entity_id,
                resolution=entities[entity_id]["approval"] if entity_id else "unconfirmed",
                **{field: credit[field] for field in ("role", "role_name", "position", "nested_position")})
        for position, value in enumerate(record["identifiers"]):
            add("identifiers", publication_id=publication_id, kind="isbn", value=value, normalized=re.sub(r"[^0-9X]", "", value.upper()), position=position)
        for position, value in enumerate(record["genres"]):
            add("genres", publication_id=publication_id, value=value, position=position)
        subjects = unmanaged_subjects(record["subjects"]) if metadata.get("classification_id") is not None else record["subjects"]
        for position, item in enumerate(subjects):
            termset = item["inDefinedTermSet"]
            add("subjects", publication_id=publication_id, name=item.get("name"), term_code=item.get("termCode"),
                set_is_url=isinstance(termset, str), set_name=termset.get("name") if isinstance(termset, dict) else None,
                set_url=termset if isinstance(termset, str) else termset.get("url"), position=position)
        for position, item in enumerate(record["audiences"]):
            add("audiences", publication_id=publication_id, kind=item["@type"], audience_type=item.get("audienceType"),
                min_age=item.get("suggestedMinAge"), max_age=item.get("suggestedMaxAge"), position=position)
        for position, modes in enumerate(record["sufficient_modes"]):
            add("sufficient_modes", md5=doc["md5"], modes=modes, position=position)
        ref = record["based_on"]
        if ref is not None:
            add("references", publication_id=publication_id, work_type=ref.get("@type"), name=ref.get("name"), language=ref.get("inLanguage"), urls=ref.get("url"))
            for position, entity in enumerate(items(ref.get("author"))):
                add("reference_authors", publication_id=publication_id, kind=entity["@type"], name=entity["name"], position=position)
    for table in ("publications", "documents", "names", "aliases", "alias_reviews", "locations", "contributions", "identifiers", "genres",
                  "subjects", "audiences", "sufficient_modes", "references", "reference_authors"):
        flush(table)
    documents = {row["md5"]: row for row in source.get("document", [])}
    for request_id, row in enumerate(source.get("library_book_previews", []), 1):
        if row["md5"] not in documents:
            raise ValueError("orphan preview checkpoint")
        private = documents[row["md5"]].get("sharing_restricted") is not False
        add("preview_requests", request_id=request_id, md5=row["md5"], idempotency_key="legacy:" + row["md5"],
            recipe=row["recipe_version"], status="failed" if private else row["status"] if row["status"] != "processing" else "pending",
            private=False, actor=actor, source_page_count=row.get("source_page_count"),
            error="Legacy public assets require regeneration into private storage" if private else row.get("error_text"))
        for role, alias in (("first", "1"), ("second", "2"), ("last", "l")):
            number = row.get(role + "_preview_page")
            if number is not None and not private:
                add("preview_pages", request_id=request_id, role=role, page_number=number,
                    small_key=f"{row['md5']}/{alias}s.webp", large_key=f"{row['md5']}/{alias}l.webp")
    flush("preview_requests")
    flush("preview_pages")
    # Review records may introduce names absent from the document metadata.
    # Explicit imported IDs must precede any sequence-generated review IDs.
    _reset_sequence(catalog, conn, "names", "name_id")
    from app.catalog.import_reviews import import_identity_reviews
    import_identity_reviews(catalog, conn, source, actor=actor)
    count = conn.execute(select(func.count()).select_from(catalog.table("documents"))).scalar_one()
    if count != report["documents"]:
        raise RuntimeError("catalog count verification failed")
    return {**report, "entities": len(entities), "publications": count, "confirmed_entities": len(approved), "actor": actor,
            "evidence_mode": evidence_mode, "source_tables": {name: {"rows": len(rows), "fingerprint": snapshot_fingerprint({name: rows})}
                for name, rows in source.items()}}
