# PostgreSQL Cutover Plan

Last updated: 2026-03-24

## Status

- PostgreSQL schema `monocorpus` is prepared.
- Manzara operational tables are created in `monocorpus`.
- SQLite operational data has been copied into PostgreSQL with row-count parity checks.
- Application runtime DB layer now uses PostgreSQL (`MANZARA_DATABASE_URL`) with configurable schema (`MANZARA_DB_SCHEMA`, default `monocorpus`).
- SQLite is no longer used by runtime or tests.

## Decisions

- Runtime target: PostgreSQL only.
- Database: same `monocorpus` database used by existing library flows.
- Target schema for Manzara operational tables: `monocorpus`.
- Migration mode: direct cutover (no dual-write phase).
- README conflict policy during remote sync: keep local Manzara README.

## Preflight Result (Current State)

Executed against local runtime database from `config.yaml`:

- `monocorpus` schema exists: **no** (initially missing)
- tables in schema `monocorpus`: **0**
- collisions with planned Manzara table names in schema `monocorpus`: **0**
- existing dataset tables are in `public` schema (`classification`, `document`, `metadata`, etc.)

Implication:
- It is safe to create Manzara operational tables in schema `monocorpus`.
- Existing library dataset queries can continue to work if search path includes `public`.

## Planned Manzara Tables in `monocorpus`

- `task_definitions`
- `panel_definitions`
- `runs`
- `run_logs`
- `events`
- `workflows`
- `workflow_steps`
- `workflow_schedules`
- `workflow_runs`
- `workflow_step_runs`
- `normalization_canonicals`
- `normalization_aliases`
- `normalization_suggestions`
- `normalization_events`

## Cutover Steps

1. Add migration tooling (Alembic) configured for PostgreSQL and schema `monocorpus`.
2. Create baseline DDL migration for all Manzara operational tables.
3. Add one-time SQLite -> PostgreSQL data migration script:
   - preserve primary keys
   - preserve timestamps and statuses
   - realign PostgreSQL sequences after import
4. Switch app runtime DB layer from SQLite to PostgreSQL:
   - use `MANZARA_DATABASE_URL` (fallback to config `database_url` only if needed)
   - use explicit schema qualification (`monocorpus.<table>`) or connection search_path
5. Run smoke checks after cutover:
   - task start/stop
   - stop-all
   - workflow scheduler
   - SSE event stream
   - flow/task rename persistence
   - normalization actions + undo
6. Remove SQLite runtime usage from app startup/config docs.

## Migration Commands (Planned Order)

1. Install dependencies:

```bash
.venv/bin/pip install -r requirements.txt
```

2. Ensure target schema exists and there are no collisions:

```bash
.venv/bin/python scripts/preflight_monocorpus_schema.py --schema monocorpus --create-schema
```

3. Create Manzara operational tables in PostgreSQL:

```bash
.venv/bin/python scripts/alembic_upgrade.py
```

4. Import current SQLite state into PostgreSQL:

```bash
.venv/bin/python scripts/migrate_sqlite_to_postgres.py --sqlite-path data/manzara.db --schema monocorpus --truncate-first
```

## Risks and Mitigations

- Table-name collisions in shared DB/schema:
  - mitigated by preflight script before DDL.
- Search path ambiguity between `monocorpus` and `public`:
  - mitigate by explicit schema qualification for Manzara tables.
- Sequence mismatch after ID-preserving imports:
  - run `setval(...)` for each serial sequence.
- Runtime regression in state transitions:
  - covered by existing API tests + smoke tests on cutover environment.

## Preflight Command

```bash
.venv/bin/python scripts/preflight_monocorpus_schema.py --schema monocorpus --create-schema
```

Use `--create-schema` only when you want the script to create the schema.
