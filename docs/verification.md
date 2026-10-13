# Verification

[Root rules](../AGENTS.md) require explicit owner requests to create, modify, or run tests. Implementation, refactoring, documentation, and commits do not authorize test work. No test suite is retained; pytest is not a runtime dependency.

## Default inspection

Review changed contracts, code, dependencies, and configuration; use `git diff --check`. For documentation, inspect local links and owner paths. Do not start the backend or apply migrations to validate docs. Static checks do not establish terminal behavior, provider connectivity, catalog mutations, recovery, or operational readiness.

## Publication-year migration verification

After applying `20261011_0066`, reconcile the configured Alembic revision, the validated `ck_catalog_publication_year` constraint, and the absence of non-null publication dates outside `YYYY` using read-only queries. The backfill commits batches of at most 500; interrupted upgrades can leave some dates normalized while the version remains at `0065`. Resume from the remaining month/day values after restoring normal writes. Inspect Aiven service disk usage separately: database size excludes WAL and other service files. Migration completion does not establish extraction/evaluation task acceptance.

## Coverage available on request

After applying the Zamanalif language revision `20261013_0068`, inspect the deployed revision, validated publication/reference language constraints, normalized language rows, generated Schema.org projections, and structured historical JSON language fields read-only. This establishes the stored spelling, not provider behavior or task acceptance.

Interactive startup migration application was reviewed through source inspection, Python syntax checks, and offline Alembic rendering. Startup waiting, advisory-lock cleanup, migration failure/uncertain-commit handling, and upgrades with autocommit index builds require explicitly requested runtime verification; implementation inspection does not establish that pending revisions have been applied to the live database.

Pending CLI acceptance covers:

- Idle launch, task/options selection, command/history pickers, settings validation, narrow-terminal resizing, and command recall. Saved task defaults need acceptance for YAML preservation, external-edit/write failures, cancel/save behavior, and restart restoration.
- Exclusive foreground ownership through finalization/output drain; history preserves that identity.
- Redacted scrollback, responsive input, honest progress/provider waits, and invalidation after runtime-read failure.
- Safe stop/restart, quit, repeated Ctrl-C exit 130, terminal restoration, and interrupted-run recovery.
- Slow/broken output: bounded backpressure, ordered transcript, coalesced redraws, final drain, and exit 1 on output errors.
- Structured artifacts independent of logs/events; approved SQLite reset and retained files; scheduled stdout flushing and finalization.

These are coverage targets, not instructions to execute them. Authorized database tests require isolated PostgreSQL matching the catalog and explicit test-only configuration; never fall back to owner credentials/config. Provider/storage/converter smoke checks require an explicit request, configured services, and reviewed cohorts. Restore drills follow [recovery](postgres-backup-recovery.md).
