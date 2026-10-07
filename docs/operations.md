# Operations

## Local storage

Artifacts use `MANZARA_ARTIFACTS_ROOT` (default `~/.manzara`). Retention groups are `cache/`, `workspaces/`, `logs/`, `state/`, `durable/`, and `private/`; `app/artifacts.py` generates `STORAGE_LAYOUT.txt` with removal guidance.

The shared source cache is `cache/source-documents`, MD5-verified and bounded by `documents.cache_max_gib`. Retained task outputs live in dedicated workspaces; task logs use `logs/task-runs/<task_id>/run-<run_id>.log`.

Local SQLite state is disposable but may be removed only with all Manzara processes stopped. It is never reconstructed from PostgreSQL. Domain checkpoints remain durable in PostgreSQL.

## Scheduled Google export

`.github/workflows/nightly-google-export.yml` runs at 00:07 UTC and supports manual dispatch. It calls `python -m app.modules.maintenance.runtime.dump_state --validate-sharing` to export the catalog to the established Drive folder and Sheets worksheet.

Actions secrets: `MANZARA_DATABASE_URL` and `GOOGLE_OAUTH_TOKEN_JSON_BASE64` (base64 OAuth token JSON with Drive/Sheets refresh authorization). Local tokens belong under `private/credentials/google-drive/`; keep them out of git and logs.

The sharing gate checks the same database snapshot used for export. Restricted-folder documents, including descendants, require `sharing_restricted=true`, an `enc:` document link when present, and blank/null `ya_public_url`. Failures report MD5/rules without links. The check uses the encryption marker and does not decrypt links.

This workflow remains in the repository; compatibility with the migrated catalog and remote configuration must be verified before claiming successful scheduled exports.

## Database tools

Alembic inspection commands: `PYTHONPATH=. .venv/bin/alembic heads` for the code chain; `current` connects to the configured database. Server startup runs pending upgrades automatically.

Historical import helpers (`scripts/migrate_sqlite_to_postgres.py`, `scripts/migrate_postgres_to_aiven.py`) are not normal setup and may assume retired schemas. Use only for an explicitly planned recovery after reviewing their code and target state.

Nightly logical dumps and restore drills: [backup and recovery](postgres-backup-recovery.md). The current backend adaptation gap is tracked in [catalog model](catalog-model.md).
