"""Owner-approved, locked forward cutover to sole local operational ownership."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.artifacts import artifacts_root
from app.operational_state import OperationalStateStore
from app.postgres_engine import get_postgres_engine
from app.settings import load_settings
from app.task_runtime.session import SessionLock

REVISION = "20261008_0062"
SOURCES = {
    "library_collection_document_features": ("library.collection_features", "md5"),
    "library_metadata_quality_state": ("library.metadata_quality", "md5"),
    "library_collection_validation_attempts": ("library.collection_validation_attempts", "attempt_id"),
    "library_book_previews": ("library.preview_history", "md5"),
    "personality_normalization_checkpoints": ("library.personality_normalization", "raw_name"),
    "library_non_pdf_extraction_state": ("library.non_pdf_extraction", "md5"),
    "document_cleanup_queue": ("maintenance.cleanup", "cleanup_id"),
    "catalog_preview_requests": ("library.preview_requests", "request_id"),
    "publisher_merge_proposals": (None, "proposal_id"),
}


def serialize(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=lambda item: item.isoformat())


def digest(value):
    return hashlib.sha256(serialize(value).encode()).hexdigest()


def local_payload(table, row):
    if table == "personality_normalization_checkpoints":
        payload = dict(row)
        if row["state"] in {"succeeded", "not_person", "unusable", "retry_requested"} or (
            row["state"] == "failed" and (row.get("failure_context") or "").startswith("Multiple compatible identities;")):
            payload.update(state="completed", failure_context=None, canonical_id=None, retryable=False,
                attempted_models={key: value for key, value in row["attempted_models"].items()
                    if not isinstance(value, dict) or value.get("kind") != "decision"})
        return payload
    if table == "document_cleanup_queue":
        return {key: row[key] for key in ("attempts", "run_id", "last_error")}
    if table == "catalog_preview_requests":
        return {"error": None if row.get("error") == "Legacy public assets require regeneration into private storage" else row.get("error")}
    if table == "library_book_previews":
        return {key: row[key] for key in ("attempt_count", "last_run_id", "error_text")}
    if table == "library_non_pdf_extraction_state":
        payload = {key: row[key] for key in ("extractor_version", "detected_format", "status", "attempt_count", "last_run_id", "error_text", "updated_at")}
        if row["status"] in {"ready", "unsupported"}:
            payload.update(status="completed", error_text=None)
        return payload
    return dict(row)


def verify_duplicates(conn, schema):
    # Tables are locked by the caller; reconstruction and privacy checks use one snapshot.
    conn.execute(text(f'SET LOCAL search_path="{schema}",public'))
    missing = conn.execute(text("""SELECT count(*) FROM library_book_previews p
        JOIN catalog_documents d USING(md5) LEFT JOIN catalog_preview_requests r
          ON r.md5=p.md5 AND r.idempotency_key='legacy:'||p.md5
        WHERE r.request_id IS NULL OR r.recipe IS DISTINCT FROM p.recipe_version
        OR r.source_page_count IS DISTINCT FROM p.source_page_count
        OR (NOT d.restricted AND (r.status IS DISTINCT FROM p.status
          OR p.first_preview_page IS DISTINCT FROM (SELECT page_number FROM catalog_preview_pages x WHERE x.request_id=r.request_id AND x.role='first')
          OR p.second_preview_page IS DISTINCT FROM (SELECT page_number FROM catalog_preview_pages x WHERE x.request_id=r.request_id AND x.role='second')
          OR p.last_preview_page IS DISTINCT FROM (SELECT page_number FROM catalog_preview_pages x WHERE x.request_id=r.request_id AND x.role='last')))
        OR (d.restricted AND r.status='ready' AND NOT r.private)""")).scalar_one()
    if missing:
        raise RuntimeError("Preview reconciliation changed; no retirement performed")
    missing = conn.execute(text("""SELECT count(*) FROM publisher_merge_proposals p
        LEFT JOIN catalog_proposals c ON c.evidence->>'source'='legacy.publisher_merge_proposals'
         AND c.evidence->>'legacy_id'=p.proposal_id::text
        WHERE c.proposal_id IS NULL OR c.evidence->'original' IS DISTINCT FROM to_jsonb(p)
        OR c.status IS DISTINCT FROM CASE p.status WHEN 'pending' THEN 'pending' WHEN 'staged' THEN 'pending'
          WHEN 'skipped' THEN 'deferred' WHEN 'applied' THEN 'applied' WHEN 'separate' THEN 'separate' ELSE 'rejected' END""")).scalar_one()
    if missing:
        raise RuntimeError("Publisher reconciliation changed; no retirement performed")
    missing = conn.execute(text("""SELECT count(*) FROM publisher_review_draft d
        CROSS JOIN LATERAL jsonb_array_elements_text(d.proposal_ids) v(id)
        WHERE NOT EXISTS(SELECT 1 FROM catalog_proposals c WHERE c.evidence->>'source'='legacy.publisher_merge_proposals'
          AND c.evidence->>'legacy_id'=v.id)""")).scalar_one()
    if missing:
        raise RuntimeError("Publisher draft references an unmatched proposal")


def recovery_dump(conn, database_url, output):
    url = make_url(database_url)
    env = os.environ.copy()
    for name in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS"):
        env.pop(name, None)
    for key, value in {"PGHOST": url.host, "PGPORT": url.port, "PGUSER": url.username,
                       "PGPASSWORD": url.password, "PGDATABASE": url.database}.items():
        if value is not None:
            env[key] = str(value)
    for key in ("sslmode", "sslrootcert", "sslcert", "sslkey"):
        if key in url.query:
            env["PG" + key.upper()] = str(url.query[key])
    env["PGCONNECT_TIMEOUT"] = "10"
    snapshot = conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
    print("Creating a consistent recovery dump while domain writes are locked", flush=True)
    dump = output / "before.dump"
    process = subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges",
                              "--snapshot=" + snapshot, "--file=" + str(dump)],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
    if process.returncode:
        raise RuntimeError("pg_dump failed; database retirement was not attempted")
    dump.chmod(0o600)
    listing = subprocess.run(["pg_restore", "--list", str(dump)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=60)
    if listing.returncode or not listing.stdout:
        raise RuntimeError("Recovery dump archive inspection failed")
    with dump.open("rb") as handle:
        archive_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    (output / "before.list").write_bytes(listing.stdout)
    (output / "before.list").chmod(0o600)
    return {"path": str(dump), "bytes": dump.stat().st_size, "sha256": archive_hash,
            "verification": "pg_restore --list; no restore drill performed"}


def transfer_local(conn, schema, store, output):
    manifest = {}
    print("Transferring and verifying local-only caches, attempts and exclusions", flush=True)
    with store.transaction() as local:
        for table, (scope, key) in SOURCES.items():
            print("Transferring " + table, flush=True)
            rows = []
            cursor = None
            while True:
                predicate = f'WHERE "{key}" > :cursor' if cursor is not None else ''
                batch = [dict(row) for row in conn.execute(text(
                    f'SELECT * FROM "{schema}"."{table}" {predicate} ORDER BY "{key}" LIMIT 1000'),
                    {"cursor": cursor} if cursor is not None else {}).mappings()]
                rows.extend(batch)
                if len(batch) < 1000:
                    break
                cursor = batch[-1][key]
            manifest[table] = {"rows": len(rows), "sha256": digest(rows)}
            if scope is None:
                continue
            store.clear(scope, conn=local)
            expected = {}
            for row in rows:
                payload = json.loads(serialize(local_payload(table, row)))
                store.put(scope, str(row[key]), payload, conn=local)
                expected[str(row[key])] = payload
            actual = {row["item_id"]: json.loads(row["payload_json"]) for row in local.execute(
                "SELECT item_id,payload_json FROM operational_items WHERE scope=? ORDER BY item_id", (scope,)).fetchall()}
            if digest(actual) != digest(expected):
                raise RuntimeError("Local transfer verification failed for " + table)
            manifest[table].update(local_scope=scope, local_sha256=digest(actual))
            print("Verified " + table + ": " + str(len(rows)) + " rows", flush=True)
    (output / "transfer.json").write_text(serialize(manifest) + "\n")
    (output / "transfer.json").chmod(0o600)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the owner-approved cutover")
    args = parser.parse_args()
    settings = load_settings()
    engine = get_postgres_engine(settings.database_url, schema=settings.database_schema, pool_size=1)
    schema = settings.database_schema
    lock = SessionLock(settings.local_state_path)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("BEGIN READ ONLY")
            revision = conn.execute(text(f'SELECT version_num FROM "{schema}".alembic_version_manzara')).scalar_one()
            before = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
            conn.rollback()
            if revision == REVISION:
                print(json.dumps({"revision": revision, "database_bytes": before, "already_applied": True}))
                return 0
            if revision != "20261007_0061":
                raise RuntimeError("Expected catalog revision 20261007_0061; inspect the deployed schema before cutover")
            if not args.apply:
                print(json.dumps({"revision": revision, "database_bytes": before, "action": "--apply required"}))
                return 0
            lock.acquire()
            store = OperationalStateStore(settings.local_state_path)
            store.initialize()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output = artifacts_root() / "durable" / "postgres-storage-cutover" / stamp
            output.mkdir(parents=True, mode=0o700)
            # Acquire all write barriers before exporting a snapshot. READ COMMITTED
            # sees any writer that committed while those barriers were acquired.
            conn.exec_driver_sql("BEGIN READ WRITE")
            conn.exec_driver_sql("SET LOCAL lock_timeout='5s'")
            conn.exec_driver_sql("SET LOCAL statement_timeout='60s'")
            conn.exec_driver_sql("SET LOCAL idle_in_transaction_session_timeout=0")
            if not conn.execute(text("SELECT pg_try_advisory_xact_lock(726193450)")).scalar_one():
                raise RuntimeError("Publisher analysis is active; stop it before storage cutover")
            active = conn.execute(text("""SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
                AND pid<>pg_backend_pid() AND backend_type='client backend' AND xact_start IS NOT NULL""")).scalar_one()
            if active:
                raise RuntimeError("Other database transactions are active; stop writers before storage cutover")
            tables = conn.execute(text("""SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname IN (:schema,'public') AND c.relkind='r' ORDER BY n.nspname,c.relname"""), {"schema": schema}).all()
            names = ','.join('"' + ns.replace('"','""') + '"."' + name.replace('"','""') + '"' for ns,name in tables)
            conn.exec_driver_sql("LOCK TABLE " + names + " IN SHARE ROW EXCLUSIVE MODE NOWAIT")
            verify_duplicates(conn, schema)
            sources = transfer_local(conn, schema, store, output)
            backup = recovery_dump(conn, settings.database_url, output)
            receipt = {"revision": REVISION, "backup": backup, "sources": sources,
                       "local_state_path": str(settings.local_state_path), "database_bytes_before": before}
            print("Applying restricted table retirement and durable/runtime column split", flush=True)
            conn.exec_driver_sql("SET LOCAL statement_timeout='60s'")
            conn.exec_driver_sql("LOCK TABLE " + names + " IN ACCESS EXCLUSIVE MODE NOWAIT")
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
            config.set_main_option("manzara_db_schema", schema)
            config.set_main_option("manzara_alembic_version_schema", schema)
            config.attributes.update(connection=conn, storage_cutover_receipt=receipt)
            command.upgrade(config, REVISION)
            conn.commit()
            conn.exec_driver_sql("BEGIN READ ONLY")
            after = conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
            count = conn.execute(text("""SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE c.relkind='r' AND n.nspname IN (:schema,'public')"""), {"schema": schema}).scalar_one()
            actual_revision = conn.execute(text(f'SELECT version_num FROM "{schema}".alembic_version_manzara')).scalar_one()
            conn.rollback()
            result = {"revision": actual_revision, "physical_tables": count,
                      "database_bytes_before": before, "database_bytes_after": after,
                      "receipt_path": str(output / "receipt.json"), "backup": backup}
            (output / "receipt.json").write_text(serialize({**receipt, "result": result}) + "\n")
            (output / "receipt.json").chmod(0o600)
            print(json.dumps(result, indent=2))
            return 0
    except Exception as exc:
        # DBAPI/SQLAlchemy exception strings can contain URLs and bound payloads.
        print(json.dumps({"error_type": type(exc).__name__,
            "message": str(exc) if isinstance(exc, (RuntimeError, ValueError)) and not hasattr(exc, "orig") else "Database or local transfer failed; no credentials logged",
            "sqlstate": getattr(getattr(exc, "orig", None), "pgcode", None)}))
        return 1
    finally:
        lock.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
