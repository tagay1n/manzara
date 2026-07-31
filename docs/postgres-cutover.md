# PostgreSQL Runtime and Migration Reference

Last updated: 2026-07-31

## Current State

- The direct SQLite-to-PostgreSQL cutover is complete.
- PostgreSQL is the only runtime state store. SQLite is not supported by application runtime or tests.
- `MANZARA_DATABASE_URL` selects the database.
- `MANZARA_DB_SCHEMA` selects the Manzara operational schema and defaults to `monocorpus`.
- Schema changes are Alembic-only. Application startup runs `upgrade head` before task and workflow definitions are seeded.
- Current Alembic head: `20260731_0013`.

The repository still contains the historical one-time import script, but it is not part of normal setup or startup. Do not run `scripts/migrate_sqlite_to_postgres.py` against an active database unless performing an explicitly planned legacy recovery.

## Schema Topology

Manzara operational tables live in the configured schema. They include task/flow definitions, runs and logs, SSE events, workflows and schedules, normalization state, Gemini runtime state, Shayan state, and Library collection/preview state.

The Monocorpus dataset tables, including `document`, may live in `public` in the same database. Connections include `public` in their search path. Migrations that extend shared dataset tables resolve the configured schema first and then the established `public` location when the configured schema is `monocorpus`.

Revision `20260731_0013` extends `document` with primary-storage verification checkpoints:

- `primary_storage_size`
- `primary_storage_etag`
- `primary_storage_verified_at`

These fields let document synchronization avoid repeated downloads and hashing when an S3 object is unchanged.

## Normal Operation

Start Manzara normally; pending migrations are applied automatically:

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload --timeout-graceful-shutdown 10
```

Apply migrations without starting the application when needed:

```bash
.venv/bin/python scripts/alembic_upgrade.py
```

Inspect the migration state:

```bash
.venv/bin/alembic current
.venv/bin/alembic heads
```

## New Database Preflight

For a new empty database only, inspect or create the operational schema before migration:

```bash
.venv/bin/python scripts/preflight_monocorpus_schema.py --schema monocorpus
.venv/bin/python scripts/preflight_monocorpus_schema.py --schema monocorpus --create-schema
```

Use `--create-schema` only when schema creation is intended. Back up existing data before any manual migration or recovery operation.

## Invariants

- Do not add runtime `CREATE TABLE` or `ALTER TABLE` statements to application source.
- Do not reintroduce SQLite configuration or dual writes.
- Keep database-data compatibility explicit in Alembic revisions.
- Ask the owner before deciding whether a persisted-data change requires compatibility or destructive cleanup.
- Verify there is exactly one Alembic head before release.
