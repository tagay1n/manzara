# Verification

[Root rules](../AGENTS.md) require explicit owner requests to create, modify, or run tests. Implementation, refactoring, documentation, and commits do not authorize test work. No test suite is retained; pytest is not a runtime dependency.

## Default inspection

Review changed contracts, code, dependencies, and configuration; use `git diff --check`. For documentation, inspect local links and owner paths. Do not start the backend or apply migrations to validate docs. Static checks do not establish terminal behavior, provider connectivity, catalog mutations, recovery, or operational readiness.

## Coverage available on request

Pending CLI acceptance covers:

- Idle launch, task/options selection, command/history pickers, settings validation, narrow-terminal resizing, and command recall.
- Exclusive foreground ownership through finalization/output drain; history preserves that identity.
- Redacted scrollback, responsive input, honest progress/provider waits, and invalidation after runtime-read failure.
- Safe stop/restart, quit, repeated Ctrl-C exit 130, terminal restoration, and interrupted-run recovery.
- Slow/broken output: bounded backpressure, ordered transcript, coalesced redraws, final drain, and exit 1 on output errors.
- Structured artifacts independent of logs/events; approved SQLite reset and retained files; scheduled stdout flushing and finalization.

These are coverage targets, not instructions to execute them. Authorized database tests require isolated PostgreSQL matching the catalog and explicit test-only configuration; never fall back to owner credentials/config. Provider/storage/converter smoke checks require an explicit request, configured services, and reviewed cohorts. Restore drills follow [recovery](postgres-backup-recovery.md).
