# Operations

## Runtime configuration

`config.yaml` is the sole local settings source. `MANZARA_CONFIG_PATH` selects another explicit file; there is no search through alternative files and no environment override of YAML values. `config.example.yaml` documents the required structure and is never runtime input. Duplicate YAML keys, masked values, missing consumed fields, and invalid setting types fail with configuration paths and without printing values. Parsing caches refresh when the selected file is edited or replaced. Stop active work and restart the process after changing settings: open pools, provider clients, and running policies retain their initialization snapshot.

Database connection/schema/version-table schema and pool size, artifact/SQLite paths, provider retry/pacing/request policies, conversion limits/deadlines, preview rendering/detection, network settings, integration targets, batch budgets, and terminal/runtime timing are explicit YAML values. Code retains domain/schema identities, formats, safety bounds, and state-machine initialization; these are implementation contracts. CLI task/cohort/retry flags are deliberate per-run controls. Policy changes govern newly performed work; they do not automatically reopen completed checkpoints or regenerate cached artifacts. Change the corresponding recipe/contract through its owner when cached output semantics must change.

`codex.publisher_merges.context_window_tokens: null` explicitly selects exact-model CLI capacity metadata; a positive integer supplies verified capacity. `database_ca_certificate_base64: null` uses the certificate path already present in `database_url`; a base64 PEM value is materialized privately by workflow setup. Dedicated backup credentials must be filled for backup work; an unconfigured integration may have explicit null fields locally and fails if invoked.

Actions are the explicit exception: operational defaults live in `.github/config/maintenance.yaml`, `export.yaml`, `backup.yaml`, and `link-checker.yaml`. `scripts/prepare_workflow_config.py PROFILE` combines these with the existing workflow credentials and writes a complete private YAML file. Application code still reads only that generated file; it has no defaults or environment overrides. Workflow setup never loads `config.example.yaml` or local `config.yaml`. No new configuration secrets or variables are required, and the maintenance secret does not need the newly introduced policy fields.

| Workflow | Existing inputs | Defaults |
| --- | --- | --- |
| Sync/cleanup and document transfer | `MANZARA_MAINTENANCE_CONFIG_BASE64`, `MANZARA_DATABASE_URL`, optional `MANZARA_AIVEN_CA_CERT_BASE64` secrets | `.github/config/maintenance.yaml` |
| Google export | `MANZARA_DATABASE_URL`, `GOOGLE_OAUTH_TOKEN_JSON_BASE64`, optional `MANZARA_AIVEN_CA_CERT_BASE64` secrets | `.github/config/export.yaml` |
| Logical backup | `MANZARA_DATABASE_URL`, `MANZARA_AIVEN_CA_CERT_BASE64`, `MANZARA_LOGICAL_BACKUP_S3_ACCESS_KEY_ID`, `MANZARA_LOGICAL_BACKUP_S3_SECRET_ACCESS_KEY` secrets; `MANZARA_LOGICAL_BACKUP_S3_ENDPOINT`, `MANZARA_LOGICAL_BACKUP_S3_REGION`, `MANZARA_LOGICAL_BACKUP_S3_BUCKET` repository variables | `.github/config/backup.yaml` |
| README link checker | No configuration secrets | `.github/config/link-checker.yaml` |

The maintenance secret continues to supply `documents.primary_storage` (endpoint, region, credentials and all five buckets), `yandex.disk` (OAuth token and document paths), and `encryption_key`. Setup ignores unrelated application sections in an older full-config secret. Supplied operational fields override maintenance defaults recursively; missing fields receive the workflow defaults. Explicit nulls and invalid values are retained and fail when consumed. Database URL and CA always come from the existing dedicated secrets; runner artifact/state/cache paths always come from the checked-in workflow profile. The maintenance cache defaults to 1 GiB; export uses a single database connection and maintenance uses four.

Keep dedicated backup credentials separate from document-storage credentials. Store private maintenance YAML and its base64 encoding under the artifact root's private credentials directory with restrictive permissions. Base64 is encoding, not encryption. For example:

```bash
umask 077
base64 -w 0 < ~/.manzara/private/credentials/maintenance/actions.yaml > ~/.manzara/private/credentials/maintenance/actions.base64
```

Workflow paths use `${RUNNER_TEMP}/manzara`; setup expands only `${RUNNER_TEMP}` in artifact/state/cache paths. An existing CA secret is materialized privately and its path replaces `sslrootcert` in the generated database URL, preserving other TLS options. Without a CA secret, maintenance/export retain the URL's TLS settings; backup requires the CA secret. Configuration and certificates are written with mode 0600 and removed at the workflow boundary. Google OAuth token provisioning remains the separate `GOOGLE_OAUTH_TOKEN_JSON_BASE64` secret.

## Local storage

Artifacts use the explicit YAML `artifacts_root`. Retention groups are `cache/`, `workspaces/`, `logs/`, `state/`, `durable/`, and `private/`; `app/artifacts.py` generates `STORAGE_LAYOUT.txt` with removal guidance.

The shared source cache is `cache/source-documents`, MD5-verified and bounded by `documents.cache_max_gib`. Retained task outputs live in dedicated workspaces. All task logs use the shared redacted stdout formatter, with immediate flushing for scheduled runs and terminal scrollback for interactive runs. No task creates log files or emits events. Existing artifact/log files are left on disk. Structured artifacts live at `workspaces/task-runs/<task_id>/run-<run_id>/artifact-<sequence>.json`; the local run summary links every artifact and the final result. Interactive controls and lifecycle are documented in [README](../README.md#controls-and-lifecycle).

Local SQLite state is disposable but may be removed only with all Manzara processes stopped. It is never reconstructed from PostgreSQL. Domain checkpoints remain durable in PostgreSQL.

## Scheduled sync and cleanup

`.github/workflows/daily-sync-cleanup.yml` (Yadisk sync) runs daily at **09:37 UTC / 12:37 Europe/Moscow**, separated from the overnight export/backup and 15:52 UTC link checker. Exact start time is unimportant; delayed schedules can still overlap. Scheduled and manual runs share a concurrency group without canceling an executing run. Sync also retains its PostgreSQL advisory lock for clients on other machines.

The workflow calls `python scripts/run_daily_maintenance.py`: cleanup preparation first, then guarded cleanup execution and Yandex Sync. Both stages use one worker and no candidate limit. Preparation failures/stops prevent Sync from starting; failed, stopped, or unfinalized runs return nonzero. ISBN duplicate groups always require an explicit persisted decision via the [cleanup review commands](document-cleanup.md). Newly discovered documents join the next daily preparation cohort.

The command uses the shared `TaskRunner`, a local session lock, run recovery, stdout logs, and directly saved structured artifacts. Read-only catalog/configuration preflight precedes preparation. SIGINT/SIGTERM request safe stop; the command also requests stop after `maintenance.daily_budget_seconds`. The Actions job allows six hours, including setup and final diagnostics; forced termination can interrupt finalization. PostgreSQL checkpoints preserve completed cleanup phases, while uncommitted discovery buffers are rebuilt on the next run. No migrations run automatically.

Provision the maintenance YAML secret using [runtime configuration](#runtime-configuration). Every run starts with fresh orchestration state at its configured runner path; cleanup plans/reviews/phases stay in PostgreSQL. The workflow installs only its dependencies selected from `requirements.txt`, without document-inference packages.

Scheduled task messages use the shared stdout formatter and flush immediately, including setup, directory listings, file visits, and per-file outcomes. At `runtime.status_interval_seconds`, a stdout `task.status` snapshot reports run state, elapsed time, time since the last task log, progress/counters, and current provider wait. A status snapshot indicates observation; advancing item/counter messages establish progress. Task artifacts contain structured results, never captured log streams.

GitHub summaries show stage outcomes/counters and overall job status. Structured task artifacts and local SQLite diagnostics are uploaded with seven-day retention, including available files after failure. Configuration, credentials, and document caches are excluded; SQLite is never restored from Actions artifacts/caches. Setup failures may have no task artifacts; inspect the Actions step output. Private configuration is removed at the final workflow boundary. Static inspection does not establish connectivity, duration, recovery behavior, or daily operational readiness; a credential-backed manual run requires explicit owner authorization.

## Backblaze document transfer

`.github/workflows/backblaze-document-transfer.yml` runs after each successful `Yadisk sync` from this repository's default branch, including syncs that discover no new files. Manual dispatch from the default branch retries/backfills independently. Both paths execute trusted default-branch code. The workflow must be present on that branch before automatic chaining works; no deployment or live execution was performed during implementation.

Upload is absent from all CLI task lists and selectors. `scripts/run_backblaze_transfer.py` is an internal Actions runner, not a supported local command. It runs `maintenance.sync_documents_s3` using the shared task runner, local session ownership/recovery, cooperative signals, and `maintenance.transfer_budget_seconds` within a six-hour Actions timeout. Long files may still be interrupted by the hard timeout; unfinished documents remain pending. Provider I/O/retries are bounded, and free disk space is checked before each download.

Use the same private maintenance YAML, primary document-storage credentials, minimal dependencies, schema, and temporary paths described above. The backup bucket role is not used. Transfer's concurrency group does not cancel an active run; shared PostgreSQL document operation locks also coordinate with sync/cleanup and clients on other machines. Different documents may proceed concurrently. Sync now requires pool size at least 3; transfer requires at least 2; both workflows use the YAML pool bound.

PostgreSQL supplies bounded missing/incomplete primary checkpoints directly, without an ID artifact from discovery. Existing complete checkpoints are excluded. The transfer verifies B2 before downloading, processes one document at a time, repairs valid incomplete checkpoints without changing their destinations, and removes owned temporary bytes at each item boundary. Invalid/protected/privacy-inconsistent checkpoints require review. The detailed catalog, integrity, and cleanup contracts live in [Maintenance storage guidance](../app/modules/maintenance/guidance/storage.md).

Redacted structured logs stream only to Actions stdout with immediate flushing; no log files or events are created. GitHub summaries report outcomes/counters. Structured result files and local SQLite diagnostics are retained for seven days, including available failure diagnostics. Configuration and document bytes are excluded, and SQLite is never restored as a transfer checkpoint. Private configuration and the transfer workspace are removed at the final workflow boundary.

A failed download, upload, cleanup, or checkpoint leaves the item unresolved; independent items continue and the job returns nonzero. Busy documents are reported as deferred, including cleanup planning when transfer owns the same document. Independent items continue; incomplete preparation prevents its following Sync stage. Empty eligible transfer work succeeds. A confirmed upload followed by a failed commit is recovered from the deterministic object on the next run without another download when sufficient identity/size evidence exists. Static inspection does not establish provider connectivity, lock/recovery behavior, duration, or production readiness; live runs require explicit owner authorization.

## Scheduled Google export

`.github/workflows/nightly-google-export.yml` (Google Sheets export) runs daily at 00:07 UTC and supports manual dispatch. Exact start time is not required; check successful daily runs. It calls `python -m app.modules.maintenance.runtime.dump_state --validate-sharing` to publish the catalog to the YAML `google.sheets` spreadsheet/worksheet target. The former `tt` worksheet is renamed in place on the first publication. No Drive archive is created or uploaded; PostgreSQL recovery dumps remain the separate [backup workflow](postgres-backup-recovery.md).

Use the existing database secret and `GOOGLE_OAUTH_TOKEN_JSON_BASE64` described in [runtime configuration](#runtime-configuration) (base64 OAuth token JSON with Sheets refresh authorization). The existing token path remains `private/credentials/google-drive/personal_token.json`; no Drive API scope is requested by this exporter. Keep tokens out of git and logs.

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

## Scheduled README link checker

The checker uses `link_checker.timeout_seconds` from `.github/config/link-checker.yaml`, materialized through the same private YAML setup. It requires no configuration secret. The workflow schedule and broken-link branch behavior remain code-owned infrastructure.
