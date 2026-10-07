#!/usr/bin/env python3
"""Reviewable offline catalog import; default invocation is read-only preflight."""

import argparse
import json

from sqlalchemy import create_engine

from app.artifacts import artifacts_root
from app.catalog.importer import import_snapshot, read_snapshot, snapshot_fingerprint, validate_snapshot
from app.catalog.repository import CatalogRepository
from app.catalog.cutover import activate_catalog
from app.catalog.search_indexes import defer_search_indexes, restore_search_indexes
from app.settings import _load_database_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-schema", default="public")
    parser.add_argument("--domain-schema", default="monocorpus")
    parser.add_argument("--target-schema", default="monocorpus")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewed-fingerprint")
    parser.add_argument("--accept-json-language", action="store_true")
    parser.add_argument("--activate", action="store_true", help="Activate task adapters after the reviewed offline import")
    parser.add_argument("--retire-legacy", action="store_true", help="Import sparse source exceptions and replace old domain tables with catalog command views")
    parser.add_argument("--defer-search-indexes", action="store_true", help="Load empty staging without search indexes, rebuilding after retirement")
    parser.add_argument("--writers-paused", action="store_true", help="Confirm all task/admin catalog writers are paused")
    parser.add_argument("--backup-verified", action="store_true", help="Confirm the pre-cutover PostgreSQL backup was verified")
    args = parser.parse_args()
    if args.activate and not (args.writers_paused and args.backup_verified):
        parser.error("activation requires --writers-paused and --backup-verified")
    if args.defer_search_indexes and not (args.retire_legacy and args.activate):
        parser.error("deferred search indexes require --retire-legacy and --activate")
    if args.retire_legacy and not (args.writers_paused and args.backup_verified):
        parser.error("retirement requires --writers-paused and --backup-verified")
    if args.activate and args.target_schema != args.domain_schema:
        parser.error("activation requires the target schema to be Manzara's domain schema")
    engine = create_engine(_load_database_url(), hide_parameters=True, pool_pre_ping=True, pool_recycle=300)
    try:
        source = read_snapshot(engine, domain_schema=args.domain_schema, dataset_schema=args.dataset_schema)
        report = validate_snapshot(source)
        fingerprint = snapshot_fingerprint(source)
        if args.apply or args.activate:
            if args.reviewed_fingerprint != fingerprint:
                raise ValueError("source differs from the reviewed preflight; stop writers and prepare a fresh review")
            engine.dispose()
            repository = CatalogRepository(engine, schema=args.target_schema)
            if args.apply:
                if args.defer_search_indexes:
                    defer_search_indexes(repository)
                report = import_snapshot(repository, source, actor="catalog-migration", allow_language_mismatches=args.accept_json_language,
                                         evidence_mode="essential" if args.retire_legacy else "full")
            if args.activate:
                engine.dispose()
                report = activate_catalog(repository, reviewed_fingerprint=fingerprint, dataset_schema=args.dataset_schema,
                                          retire_legacy=args.retire_legacy)
                if args.defer_search_indexes:
                    restore_search_indexes(repository)
        destination = artifacts_root() / "durable" / "catalog-migrations"
        destination.mkdir(parents=True, exist_ok=True)
        manifest = {"fingerprint": fingerprint, "applied": args.apply, "activated": args.activate, **report}
        suffix = "-activated.json" if args.activate else "-applied.json" if args.apply else "-preflight.json"
        path = destination / (fingerprint + suffix)
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        print(json.dumps({"fingerprint": fingerprint, "applied": args.apply, "activated": args.activate, "documents": report["documents"],
                          "language_mismatches": len(report["language_mismatches"]), "manifest": str(path)}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
