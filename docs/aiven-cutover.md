# Aiven PostgreSQL cutover

Manzara can fit its durable production state in Aiven's 1 GiB PostgreSQL tier
only when verbose runtime telemetry is excluded. The migration keeps every
table in `monocorpus` and `public`, all compact `runs` rows, and every event
except historical `task.log` and `task.progress`. It copies no `run_logs` data
and never changes the source database.

## Security prerequisites

Rotate any database password that has appeared in chat, terminal history, or a
log. Download the Aiven project CA outside the repository, for example under
`~/.manzara/credentials/`, and use `sslmode=verify-full` with `sslrootcert` in
the target URL. Keep both URLs out of command arguments and repository files.

The target must be empty, offer `pg_trgm`, run the same or a newer PostgreSQL
major version than the source, and contain no user tables in `monocorpus` or
`public`.

## Preflight

Stop Manzara gracefully and confirm that every task and conveyor run is at a
terminal boundary. Provide the URLs only through the process environment:

```bash
export MANZARA_SOURCE_DATABASE_URL='<local PostgreSQL URL>'
export MANZARA_TARGET_DATABASE_URL='<rotated Aiven URL with verify-full and sslrootcert>'
PYTHONPATH=. .venv/bin/python scripts/migrate_postgres_to_aiven.py --preflight
```

Preflight refuses an active source, an occupied or older target, a missing
extension, or a selected source footprint above 750 MiB. Its output contains
hostnames, database names, versions, sizes, and counts, but never credentials.

## Apply and verify

```bash
PYTHONPATH=. .venv/bin/python scripts/migrate_postgres_to_aiven.py --apply
```

The helper creates a timestamped directory under
`~/.manzara/migrations/`, uses a temporary mode-0600 libpq service file, runs a
parallel directory-format dump, restores in single-worker schema, data, and
post-data phases to limit free-tier WAL pressure, copies retained events,
advances the event identity sequence, runs `ANALYZE`, and verifies row counts.
A masked `migration-manifest.json` is the completion record.

After a successful restore, configure the runtime with the rotated target URL,
`MANZARA_DB_SCHEMA=monocorpus`, and
`MANZARA_POSTGRES_BACKUP_MODE=managed`. Run the normal application startup so
Alembic verifies head and definitions are seeded. Confirm dashboard reads, SSE,
file-log pagination, task stop/recovery, and one representative workflow before
allowing routine writes.

Do not delete or upgrade the local source during the rollback window. Before
new cloud writes begin, rollback is a configuration switch. After new writes
begin, preserve the target and plan a reverse migration rather than blindly
switching to the stale local database.
