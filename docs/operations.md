# Operations

## Local storage

Artifacts use `MANZARA_ARTIFACTS_ROOT` (default `~/.manzara`). Retention groups are `cache/`, `workspaces/`, `logs/`, `state/`, `durable/`, and `private/`; `app/artifacts.py` generates `STORAGE_LAYOUT.txt` with removal guidance.

The shared source cache is `cache/source-documents`, MD5-verified and bounded by `documents.cache_max_gib`. Retained task outputs live in dedicated workspaces. All task logs use the shared redacted stdout formatter, with immediate flushing for scheduled runs and terminal scrollback for interactive runs. No task creates log files or emits events. Existing artifact/log files are left on disk. Structured artifacts live at `workspaces/task-runs/<task_id>/run-<run_id>/artifact-<sequence>.json`; the local run summary links every artifact and the final result. Interactive controls and lifecycle are documented in [README](../README.md#controls-and-lifecycle).

Local SQLite state is disposable but may be removed only with all Manzara processes stopped. It is never reconstructed from PostgreSQL. Domain checkpoints remain durable in PostgreSQL.

## Scheduled sync and cleanup

`.github/workflows/daily-sync-cleanup.yml` (Yadisk sync) runs daily at **09:37 UTC / 12:37 Europe/Moscow**, separated from the overnight export/backup and 15:52 UTC link checker. Exact start time is unimportant; delayed schedules can still overlap. Scheduled and manual runs share a concurrency group without canceling an executing run. Sync also retains its PostgreSQL advisory lock for clients on other machines.

The workflow calls `python scripts/run_daily_maintenance.py`: cleanup preparation first, then guarded cleanup execution and Yandex Sync. Both stages use one worker and no candidate limit. Preparation failures/stops prevent Sync from starting; failed, stopped, or unfinalized runs return nonzero. ISBN duplicate groups always require an explicit persisted decision via the [cleanup review commands](document-cleanup.md). Newly discovered documents join the next daily preparation cohort.

The command uses the shared `TaskRunner`, a local session lock, run recovery, stdout logs, and directly saved structured artifacts. Read-only catalog/configuration preflight precedes preparation. SIGINT/SIGTERM request safe stop; the command also requests stop after a five-hour execution budget. The Actions job allows six hours, including setup and final diagnostics; forced termination can interrupt finalization. PostgreSQL checkpoints preserve completed cleanup phases, while uncommitted discovery buffers are rebuilt on the next run. No migrations run automatically.

Add these Actions secrets manually:

| Secret | Value |
| --- | --- |
| `MANZARA_DATABASE_URL` | Existing primary PostgreSQL URL, with the intended TLS settings |
| `MANZARA_MAINTENANCE_CONFIG_BASE64` | Single-line base64 of maintenance-only YAML described below |
| `MANZARA_AIVEN_CA_CERT_BASE64` | Optional existing base64 PEM CA when the database requires a provider certificate |

Build the maintenance-only YAML from the corresponding sections of [config.example.yaml](../config.example.yaml), replacing masked values with actual values. Retain `documents.primary_storage` (endpoint, region, access key, secret key, and all five bucket names: `public`, `private`, `book_previews`, `content`, `content_images`), `yandex.disk` (OAuth token and `documents.source_path`, `restricted_path`, `filtered_out_path`), and `encryption_key`. Use document-storage credentials authorized for the managed cleanup objects; the logical-backup bucket credentials are a separate role. Exclude Gemini keys, Codex settings, and unrelated credentials. Base64 is encoding, not encryption; keep the source and encoded file under the private artifact root with restrictive permissions, never in git or logs.

For example, after creating `~/.manzara/private/credentials/maintenance/actions.yaml` with that subset:

```bash
umask 077
base64 -w 0 < ~/.manzara/private/credentials/maintenance/actions.yaml > ~/.manzara/private/credentials/maintenance/actions.base64
```

Paste the encoded file into `MANZARA_MAINTENANCE_CONFIG_BASE64`. No additional repository variables are required. `scripts/prepare_maintenance_config.py` selects only the maintenance fields, masks decoded credentials in Actions output, writes a private YAML file, and overrides the cache path/budget for the temporary runner. Missing/masked credentials or bucket names fail setup. When a CA is supplied, it writes the certificate privately and replaces the URL's `sslrootcert` path for the runner, preserving other TLS settings.

The runner initializes artifact/cache/configuration/local SQLite paths under `$RUNNER_TEMP/manzara` through `$GITHUB_ENV`, with schema `monocorpus` and pool size 4. Every run starts with fresh orchestration state; cleanup plans/reviews/phases stay in PostgreSQL. The workflow installs only its dependencies selected from `requirements.txt`, without document-inference packages.

Scheduled task messages use the shared stdout formatter and flush immediately, including setup, directory listings, file visits, and per-file outcomes. Every 30 seconds, a stdout `task.status` snapshot reports run state, elapsed time, time since the last task log, progress/counters, and current provider wait. A status snapshot indicates observation; advancing item/counter messages establish progress. Task artifacts contain structured results, never captured log streams.

GitHub summaries show stage outcomes/counters and overall job status. Structured task artifacts and local SQLite diagnostics are uploaded with seven-day retention, including available files after failure. Configuration, credentials, and document caches are excluded; SQLite is never restored from Actions artifacts/caches. Setup failures may have no task artifacts; inspect the Actions step output. Private configuration is removed at the final workflow boundary. Static inspection does not establish connectivity, duration, recovery behavior, or daily operational readiness; a credential-backed manual run requires explicit owner authorization.

## Backblaze document transfer

`.github/workflows/backblaze-document-transfer.yml` runs after each successful `Yadisk sync` from this repository's default branch, including syncs that discover no new files. Manual dispatch from the default branch retries/backfills independently. Both paths execute trusted default-branch code. The workflow must be present on that branch before automatic chaining works; no deployment or live execution was performed during implementation.

Upload is absent from all CLI task lists and selectors. `scripts/run_backblaze_transfer.py` is an internal Actions runner, not a supported local command. It runs `maintenance.sync_documents_s3` using the shared task runner, local session ownership/recovery, cooperative signals, and a five-hour budget within a six-hour Actions timeout. Long files may still be interrupted by the hard timeout; unfinished documents remain pending. Provider I/O/retries are bounded, and free disk space is checked before each download.

Reuse the maintenance secrets, private configuration preparation, primary document-storage credentials, minimal dependencies, schema, and temporary paths described above. The backup bucket role is not used. Transfer's concurrency group does not cancel an active run; shared PostgreSQL document operation locks also coordinate with sync/cleanup and clients on other machines. Different documents may proceed concurrently. Sync now requires pool size at least 3; transfer requires at least 2; both workflows use 4.

PostgreSQL supplies bounded missing/incomplete primary checkpoints directly, without an ID artifact from discovery. Existing complete checkpoints are excluded. The transfer verifies B2 before downloading, processes one document at a time, repairs valid incomplete checkpoints without changing their destinations, and removes owned temporary bytes at each item boundary. Invalid/protected/privacy-inconsistent checkpoints require review. The detailed catalog, integrity, and cleanup contracts live in [Maintenance storage guidance](../app/modules/maintenance/guidance/storage.md).

Redacted structured logs stream only to Actions stdout with immediate flushing; no log files or events are created. GitHub summaries report outcomes/counters. Structured result files and local SQLite diagnostics are retained for seven days, including available failure diagnostics. Configuration and document bytes are excluded, and SQLite is never restored as a transfer checkpoint. Private configuration and the transfer workspace are removed at the final workflow boundary.

A failed download, upload, cleanup, or checkpoint leaves the item unresolved; independent items continue and the job returns nonzero. Busy documents are reported as deferred, including cleanup planning when transfer owns the same document. Independent items continue; incomplete preparation prevents its following Sync stage. Empty eligible transfer work succeeds. A confirmed upload followed by a failed commit is recovered from the deterministic object on the next run without another download when sufficient identity/size evidence exists. Static inspection does not establish provider connectivity, lock/recovery behavior, duration, or production readiness; live runs require explicit owner authorization.

## Scheduled Google export

`.github/workflows/nightly-google-export.yml` (Google Sheets export) runs daily at 00:07 UTC and supports manual dispatch. Exact start time is not required; check successful daily runs. It calls `python -m app.modules.maintenance.runtime.dump_state --validate-sharing` to publish the catalog to the established spreadsheet's `documents` worksheet. The former `tt` worksheet is renamed in place on the first publication. No Drive archive is created or uploaded; PostgreSQL recovery dumps remain the separate [backup workflow](postgres-backup-recovery.md).

Actions secrets: `MANZARA_DATABASE_URL` and `GOOGLE_OAUTH_TOKEN_JSON_BASE64` (base64 OAuth token JSON with Sheets refresh authorization). The existing token path remains `private/credentials/google-drive/personal_token.json`; no Drive API scope is requested by this exporter. Keep tokens out of git and logs.

The sharing gate checks the same database snapshot used for export. Restricted-folder documents, including descendants, require `sharing_restricted=true`, an `enc:` document link when present, and blank/null `ya_public_url`. Failures report MD5/rules without links. The check uses the encryption marker and does not decrypt links.

The shared reader in `app/catalog/export.py` reads normalized tables in bounded batches within one read-only, repeatable-read snapshot and uses the shared metadata renderer. It does not read legacy `document`/`metadata` views or migrate persisted data. The complete worksheet is prepared and its cell lengths validated before remote replacement.

Columns, in order: `md5`, `mime_type`, `ya_path`, `ya_public_url`, `publisher`, `author`, `title`, `isbn`, `publish_year`, `language`, `translated`, `page_count`, `full`, `sharing_restricted`, `document_url`, `content_url`, `meta`, `size`. `meta` is generated Schema.org JSON from relational facts, preserving full metadata rather than exposing storage JSON as a domain owner. `size` comes from the primary S3 location's persisted byte size, uses binary units (`B`, `KiB`, `MiB`, etc.), and stays blank when unknown. Publication writes RAW values to preserve JSON, identifiers, and literal text.

GitHub summaries record successful row/column counts or a Sheets publication failure. Code inspection alone does not establish scheduled execution; check the deployed workflow's run history after publishing a change.

## Local runtime state

Local SQLite schema version 8 removes event storage, worker counts, and retired Gemini UI control fields. The owner approved fresh local state: the first initialization of an older runtime database clears its local run history, Gemini coordination, retry exclusions, flow attempts/errors, and caches stored in SQLite, then creates the current schema. PostgreSQL domain data and durable checkpoints, artifact files, and source-document cache files are unaffected. Subsequent startups at version 8 retain their local state. Unknown future schema versions are rejected.

Task registrations live in Python code; the runner records explicit run options and executes Python handlers. Progress and provider waits use direct run snapshots. Tasks emit no lifecycle, artifact, or Gemini events. Structured JSON results are referenced by run summaries. The retained `panel_id` field denotes the task group.

## Database tools

Alembic inspection commands: `PYTHONPATH=. .venv/bin/alembic heads` for the code chain; `current` connects to the configured database. CLI startup initializes only SQLite; durable upgrades are separate operations.

The active history is one baseline, `20261008_0062`, with no parent. Its frozen SQL defines the current durable schema, including views, functions, triggers, constraints, and `pg_trgm` in `public`. It creates no corpus rows or local runtime data. A new database requires PostgreSQL 18 or newer, UTF-8, an empty catalog schema, and no existing `public.document`, `public.metadata`, `public.classification`, or `public.isbn_keep_many`. Configure the database URL/schema, then run `PYTHONPATH=. .venv/bin/alembic upgrade head` separately from the CLI. Bootstrap is transactional and refuses existing domain objects. The migration role needs permission to create schemas and install `pg_trgm`, or an administrator must preinstall that extension in `public`.

A database already at `20261008_0062` keeps its revision and data: `upgrade head` has no domain migration to apply. Do not restamp it. Future migrations descend from this baseline. The two migration-only adapter installer functions are excluded from fresh bootstrap; deployed databases need no cleanup of those functions.

Upgrade support below `0062` is retired in this checkout. For an older database, use commit `c9782d1` and its original chain to reach `0062` before switching to the baseline checkout. In that historical checkout, `scripts/reduce_postgres_storage.py --apply` performs the final verified local transfer and recovery dump; ordinary upgrade cannot bypass its receipt. Never stamp an older or populated unversioned schema to pretend it matches the baseline. Restoring an older dump also requires the historical code and a reviewed upgrade plan.

Existing storage-cutover recovery artifacts remain under `durable/postgres-storage-cutover/<timestamp>/`; the PostgreSQL evidence receipt records their hashes and paths. Archive listing is verified; this does not constitute a restore drill. Recovery requires an explicitly planned dump restore, never an automatic store fallback. Historical catalog import/activation and manual SQL helpers require their original schema and installer functions; use the corresponding historical checkout for reviewed recovery, not the baseline bootstrap.

Historical import, migration, and manual SQL tools are absent from this checkout. Retrieve the corresponding historical code only for explicitly planned recovery after reviewing its schema assumptions and target state.

Nightly logical dumps and restore drills: [backup and recovery](postgres-backup-recovery.md). The current backend adaptation gap is tracked in [catalog model](catalog-model.md).
