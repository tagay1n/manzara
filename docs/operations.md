# Operations

## Local storage

Artifacts use `MANZARA_ARTIFACTS_ROOT` (default `~/.manzara`). Retention groups are `cache/`, `workspaces/`, `logs/`, `state/`, `durable/`, and `private/`; `app/artifacts.py` generates `STORAGE_LAYOUT.txt` with removal guidance.

The shared source cache is `cache/source-documents`, MD5-verified and bounded by `documents.cache_max_gib`. Retained task outputs live in dedicated workspaces; task logs use `logs/task-runs/<task_id>/run-<run_id>.log`.

Local SQLite state is disposable but may be removed only with all Manzara processes stopped. It is never reconstructed from PostgreSQL. Domain checkpoints remain durable in PostgreSQL.

## Scheduled Google export

`.github/workflows/nightly-google-export.yml` (Nightly Google Sheets Export) runs daily at 00:07 UTC and supports manual dispatch. Exact start time is not required; check successful daily runs. It calls `python -m app.modules.maintenance.runtime.dump_state --validate-sharing` to publish the catalog to the established spreadsheet's `documents` worksheet. The former `tt` worksheet is renamed in place on the first publication. No Drive archive is created or uploaded; PostgreSQL recovery dumps remain the separate [backup workflow](postgres-backup-recovery.md).

Actions secrets: `MANZARA_DATABASE_URL` and `GOOGLE_OAUTH_TOKEN_JSON_BASE64` (base64 OAuth token JSON with Sheets refresh authorization). The existing token path remains `private/credentials/google-drive/personal_token.json`; no Drive API scope is requested by this exporter. Keep tokens out of git and logs.

The sharing gate checks the same database snapshot used for export. Restricted-folder documents, including descendants, require `sharing_restricted=true`, an `enc:` document link when present, and blank/null `ya_public_url`. Failures report MD5/rules without links. The check uses the encryption marker and does not decrypt links.

The shared reader in `app/catalog/export.py` reads normalized tables in bounded batches within one read-only, repeatable-read snapshot and uses the shared metadata renderer. It does not read legacy `document`/`metadata` views or migrate persisted data. The complete worksheet is prepared and its cell lengths validated before remote replacement.

Columns, in order: `md5`, `mime_type`, `ya_path`, `ya_public_url`, `publisher`, `author`, `title`, `isbn`, `publish_year`, `language`, `translated`, `page_count`, `full`, `sharing_restricted`, `document_url`, `content_url`, `meta`, `size`. `meta` is generated Schema.org JSON from relational facts, preserving full metadata rather than exposing storage JSON as a domain owner. `size` comes from the primary S3 location's persisted byte size, uses binary units (`B`, `KiB`, `MiB`, etc.), and stays blank when unknown. Publication writes RAW values to preserve JSON, identifiers, and literal text.

GitHub summaries record successful row/column counts or a Sheets publication failure. Code inspection alone does not establish scheduled execution; check the deployed workflow's run history after publishing a change.

## Database tools

Alembic inspection commands: `PYTHONPATH=. .venv/bin/alembic heads` for the code chain; `current` connects to the configured database. CLI startup initializes only SQLite; durable upgrades are separate operations.

The active history is one baseline, `20261008_0062`, with no parent. Its frozen SQL defines the current durable schema, including views, functions, triggers, constraints, and `pg_trgm` in `public`. It creates no corpus rows or local runtime data. A new database requires PostgreSQL 18 or newer, UTF-8, an empty catalog schema, and no existing `public.document`, `public.metadata`, `public.classification`, or `public.isbn_keep_many`. Configure the database URL/schema, then run `PYTHONPATH=. .venv/bin/alembic upgrade head` separately from the CLI. Bootstrap is transactional and refuses existing domain objects. The migration role needs permission to create schemas and install `pg_trgm`, or an administrator must preinstall that extension in `public`.

A database already at `20261008_0062` keeps its revision and data: `upgrade head` has no domain migration to apply. Do not restamp it. Future migrations descend from this baseline. The two migration-only adapter installer functions are excluded from fresh bootstrap; deployed databases need no cleanup of those functions.

Upgrade support below `0062` is retired in this checkout. For an older database, use commit `c9782d1` and its original chain to reach `0062` before switching to the baseline checkout. In that historical checkout, `scripts/reduce_postgres_storage.py --apply` performs the final verified local transfer and recovery dump; ordinary upgrade cannot bypass its receipt. Never stamp an older or populated unversioned schema to pretend it matches the baseline. Restoring an older dump also requires the historical code and a reviewed upgrade plan.

Existing storage-cutover recovery artifacts remain under `durable/postgres-storage-cutover/<timestamp>/`; the PostgreSQL evidence receipt records their hashes and paths. Archive listing is verified; this does not constitute a restore drill. Recovery requires an explicitly planned dump restore, never an automatic store fallback. Historical catalog import/activation and manual SQL helpers require their original schema and installer functions; use the corresponding historical checkout for reviewed recovery, not the baseline bootstrap.

Historical import helpers (`scripts/migrate_sqlite_to_postgres.py`, `scripts/migrate_postgres_to_aiven.py`) are not normal setup and may assume retired schemas. Use only for an explicitly planned recovery after reviewing their code and target state.

Nightly logical dumps and restore drills: [backup and recovery](postgres-backup-recovery.md). The current backend adaptation gap is tracked in [catalog model](catalog-model.md).
