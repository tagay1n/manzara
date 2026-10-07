"""Explicit, locked transition from the reviewed import to task adapters."""

import re

from sqlalchemy import select, text

from app.catalog.importer import SOURCE_TABLES, snapshot_fingerprint
from app.catalog.import_evidence import RETIRED_FIELDS


def activate_catalog(catalog, *, reviewed_fingerprint, dataset_schema="public", retire_legacy=False):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dataset_schema):
        raise ValueError("invalid dataset schema")
    schema = catalog.schema
    with catalog.engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '10s'"))
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"catalog-import:{schema}"})
        imports = catalog.table("imports")
        imported = conn.execute(select(imports).where(imports.c.source_fingerprint == reviewed_fingerprint).with_for_update()).mappings().first()
        if imported is None:
            raise ValueError("reviewed snapshot has not been imported")
        if imported["state"] == "active":
            if imported["manifest"].get("dataset_schema") != dataset_schema:
                raise ValueError("catalog was activated against a different dataset schema")
            if retire_legacy and not imported["manifest"].get("legacy_retired"):
                raise ValueError("active stored projections require a separate reviewed retirement")
            return imported["manifest"]
        if imported['state'] != 'verified':
            raise ValueError('catalog import is not verified; complete and verify loading before activation')
        if retire_legacy and imported["manifest"].get("evidence_mode") != "essential":
            raise ValueError("retirement requires a reviewed essential-evidence import")
        source = {}
        for name in SOURCE_TABLES:
            owner = dataset_schema if name in {"document", "metadata", "classification", "isbn_keep_many"} else schema
            relation = f'"{owner}"."{name}"'
            if conn.execute(text("SELECT to_regclass(:relation)"), {"relation": relation}).scalar() is None:
                continue
            conn.execute(text(f"LOCK TABLE {relation} IN SHARE ROW EXCLUSIVE MODE"))
            query = text(f"SELECT * FROM {relation}").execution_options(yield_per=1024)
            source[name] = [dict(row) for row in conn.execute(query).mappings()]
        if snapshot_fingerprint(source) != reviewed_fingerprint:
            raise ValueError("source changed since import; keep writers paused and prepare a fresh import")
        for table in sorted(catalog.tables.values(), key=lambda item: item.name):
            conn.execute(text(f'LOCK TABLE "{schema}"."{table.name}" IN SHARE ROW EXCLUSIVE MODE'))
        if conn.execute(select(catalog.table("revisions")).limit(1)).first():
            raise ValueError("staging catalog was edited; reconcile it before cutover")
        for table in catalog.tables.values():
            if "revision" in table.c and conn.execute(select(table).where(table.c.revision != 1).limit(1)).first():
                raise ValueError("staging catalog was edited; reconcile it before cutover")
        if retire_legacy:
            _verify_retirement(conn, catalog, source, dataset_schema)
            _retire_tables(conn, schema, dataset_schema)
            conn.execute(text(f'SELECT "{schema}".catalog_install_lean_adapters(:dataset)'), {"dataset": dataset_schema})
        else:
            conn.execute(text(f'SELECT "{schema}".catalog_activate_task_adapters(:dataset)'), {"dataset": dataset_schema})
        manifest = {**imported["manifest"], "dataset_schema": dataset_schema, "legacy_retired": retire_legacy}
        conn.execute(imports.update().where(imports.c.import_id == imported["import_id"]).values(state="active", manifest=manifest))
        return manifest


def _verify_retirement(conn, catalog, source, dataset_schema):
    """Check imported identities and assignments independently before any drop."""
    from sqlalchemy import func

    expected = {"documents": "document", "publications": "document", "entities": "normalization_canonicals",
                "alias_reviews": "normalization_aliases", "classifications": "classification", "collections": "library_collections"}
    for target, original in expected.items():
        count = conn.execute(select(func.count()).select_from(catalog.table(target))).scalar_one()
        if count != len(source.get(original, [])):
            raise ValueError(f"retirement verification failed for {target}; source tables retained")
    domain = f'"{catalog.schema}"'
    dataset = f'"{dataset_schema}"'
    checks = {
        "document mapping": f'''SELECT count(*) FROM {dataset}.document old
            FULL JOIN {domain}.catalog_documents d USING(md5)
            WHERE old.md5 IS NULL OR d.md5 IS NULL OR old.mime_type IS DISTINCT FROM d.mime_type
            OR (old."full" IS TRUE) IS DISTINCT FROM d.complete
            OR (old.sharing_restricted IS NOT FALSE) IS DISTINCT FROM d.restricted
            OR old.content_extraction_method IS DISTINCT FROM d.content_extraction_method
            OR old.meta_extraction_method IS DISTINCT FROM d.meta_extraction_method''',
        "metadata assignment": f'''SELECT count(*) FROM {domain}.catalog_documents d
            JOIN {domain}.catalog_publications p USING(publication_id) LEFT JOIN {dataset}.metadata old USING(md5)
            WHERE p.has_metadata IS DISTINCT FROM (old.md5 IS NOT NULL)
            OR p.metadata_present IS DISTINCT FROM (old.schema_org IS NOT NULL)
            OR p.inclusion IS DISTINCT FROM CASE old.lib WHEN TRUE THEN 'included' WHEN FALSE THEN 'excluded' ELSE 'pending' END
            OR p.evaluation_method IS DISTINCT FROM old.lib_eval_method
            OR p.classification_id IS DISTINCT FROM old.classification_id''',
        "collection assignment": f'''SELECT count(*) FROM {domain}.catalog_documents d
            JOIN {domain}.catalog_publications p USING(publication_id)
            LEFT JOIN {domain}.library_collection_items old USING(md5)
            WHERE p.collection_id IS DISTINCT FROM old.collection_id
            OR p.collection_item_title IS DISTINCT FROM old.item_title
            OR p.collection_created_at IS DISTINCT FROM old.created_at
            OR p.collection_updated_at IS DISTINCT FROM old.updated_at''',
        "alias review mapping": f'''SELECT count(*) FROM {domain}.normalization_aliases old
            FULL JOIN {domain}.catalog_alias_reviews r USING(alias_id)
            LEFT JOIN {domain}.catalog_names n ON n.name_id=r.name_id
            WHERE old.alias_id IS NULL OR r.alias_id IS NULL OR old.raw_name IS DISTINCT FROM n.raw_name
            OR old.canonical_id IS DISTINCT FROM r.entity_id OR old.decision_status IS DISTINCT FROM r.decision_status
            OR old.successful_model IS DISTINCT FROM r.successful_model''',
    }
    for label, query in checks.items():
        if conn.execute(text(query)).scalar_one():
            raise ValueError(f"retirement verification failed for {label}; source tables retained")


def _retire_tables(conn, schema, dataset_schema):
    """Repoint retained foreign keys, then explicit RESTRICT drops in one transaction."""
    replacements = {
        "document": ("catalog_documents", {"md5": "md5"}),
        "metadata": None,
        "classification": ("catalog_classifications", {"id": "classification_id"}),
        "normalization_canonicals": ("catalog_entities", {"canonical_id": "entity_id"}),
        "normalization_aliases": ("catalog_alias_reviews", {"alias_id": "alias_id"}),
        "library_collections": ("catalog_collections", {"collection_id": "collection_id"}),
        "library_collection_items": None,
    }
    retired = {(dataset_schema if name in {"document", "metadata", "classification"} else schema, name)
               for name in RETIRED_FIELDS}
    constraints = conn.execute(text('''SELECT k.conname,k.confupdtype,k.confdeltype,k.confmatchtype,k.condeferrable,k.condeferred,
        ns.nspname AS owner_schema,r.relname AS owner_table,tn.nspname AS target_schema,t.relname AS target_table,
        ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY x(id,pos)
              JOIN pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=x.id ORDER BY pos) AS columns,
        ARRAY(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY x(id,pos)
              JOIN pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=x.id ORDER BY pos) AS target_columns
        FROM pg_constraint k JOIN pg_class r ON r.oid=k.conrelid JOIN pg_namespace ns ON ns.oid=r.relnamespace
        JOIN pg_class t ON t.oid=k.confrelid JOIN pg_namespace tn ON tn.oid=t.relnamespace WHERE k.contype='f'
        AND tn.nspname IN (:domain,:dataset)'''), {"domain": schema, "dataset": dataset_schema}).mappings().all()
    quote = conn.dialect.identifier_preparer.quote_identifier
    actions = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
    for row in constraints:
        target = (row["target_schema"], row["target_table"])
        owner = (row["owner_schema"], row["owner_table"])
        if target not in retired:
            continue
        if owner[0] not in {schema, dataset_schema}:
            raise ValueError("unowned schema depends on a retired table; review its foreign key first")
        relation = f'{quote(owner[0])}.{quote(owner[1])}'
        conn.execute(text(f'ALTER TABLE {relation} DROP CONSTRAINT {quote(row["conname"])}'))
        if owner in retired:
            continue
        replacement = replacements[row["target_table"]]
        if replacement is None:
            raise ValueError(f"retained table {owner[1]} requires an explicit foreign-key migration")
        table, mapping = replacement
        if any(name not in mapping for name in row["target_columns"]):
            raise ValueError("unexpected retired foreign-key columns")
        columns = ",".join(map(quote, row["columns"]))
        targets = ",".join(quote(mapping[name]) for name in row["target_columns"])
        match = {"s": "SIMPLE", "f": "FULL", "p": "PARTIAL"}[row["confmatchtype"]]
        deferred = (" DEFERRABLE INITIALLY " + ("DEFERRED" if row["condeferred"] else "IMMEDIATE")) if row["condeferrable"] else " NOT DEFERRABLE"
        conn.execute(text(f'''ALTER TABLE {relation} ADD CONSTRAINT {quote(row["conname"])}
            FOREIGN KEY ({columns}) REFERENCES {quote(schema)}.{quote(table)} ({targets}) MATCH {match}
            ON UPDATE {actions[row["confupdtype"]]} ON DELETE {actions[row["confdeltype"]]}{deferred}'''))
    # Unknown views/functions that depend on a retired table make these drops
    # fail; CASCADE must never silently remove someone else's durable state.
    for name in ("library_collection_items", "normalization_aliases", "metadata", "classification",
                 "library_collections", "normalization_canonicals", "document"):
        owner = dataset_schema if name in {"document", "metadata", "classification"} else schema
        if conn.execute(text("SELECT to_regclass(:name)"), {"name": f'{quote(owner)}.{quote(name)}'}).scalar() is not None:
            conn.execute(text(f'DROP TABLE {quote(owner)}.{quote(name)} RESTRICT'))
