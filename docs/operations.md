# Operations

## Runtime configuration

Selected private YAML is the sole settings source: `config.yaml` or the explicit `MANZARA_CONFIG_PATH`, with no alternative-file search or environment value overrides. [config.example.yaml](../config.example.yaml) is masked structure only. Duplicate keys, masks, missing consumed fields, and invalid types fail with paths without printing values. Parsing refreshes after edits; stop work and restart because initialized pools/clients/policies retain their snapshots.

Database/TLS/schema/pool, artifact/SQLite paths, provider policies, converter/preview limits, integrations, budgets, and timing are explicit YAML. CLI cohort/retry flags are per-run controls. Policy changes do not reopen completed checkpoints or regenerate cached output; change the owning recipe/contract when semantics change. Unconfigured integrations may use explicit nulls and fail if invoked. `codex.publisher_merges.context_window_tokens: null` uses exact-model CLI capacity metadata; a positive integer supplies verified capacity. `database_ca_certificate_base64: null` retains the URL's certificate path; a PEM value is privately materialized by setup.

Actions use `scripts/prepare_workflow_config.py PROFILE` to combine `.github/config/` defaults with existing credentials into private YAML. Setup never loads the local/example config.

| Profile / workflows | Credentials and inputs |
| --- | --- |
| `maintenance`: daily cleanup/sync and transfer | Secrets `MANZARA_MAINTENANCE_CONFIG_BASE64`, `MANZARA_DATABASE_URL`, optional `MANZARA_AIVEN_CA_CERT_BASE64` |
| `export`: Google Sheets | Secrets `MANZARA_DATABASE_URL`, `GOOGLE_OAUTH_TOKEN_JSON_BASE64`, optional `MANZARA_AIVEN_CA_CERT_BASE64` |
| `backup`: logical dumps | Secrets `MANZARA_DATABASE_URL`, `MANZARA_AIVEN_CA_CERT_BASE64`, `MANZARA_LOGICAL_BACKUP_S3_ACCESS_KEY_ID`, `MANZARA_LOGICAL_BACKUP_S3_SECRET_ACCESS_KEY`; variables `MANZARA_LOGICAL_BACKUP_S3_ENDPOINT`, `MANZARA_LOGICAL_BACKUP_S3_REGION`, `MANZARA_LOGICAL_BACKUP_S3_BUCKET` |
| `link-checker` | No configuration secrets |

Maintenance YAML supplies `documents.primary_storage` (endpoint, region, credentials, five buckets), `yandex.disk` (token/paths), and `encryption_key`. Supplied policy fields recursively override profile defaults; explicit nulls/invalid values remain and fail when consumed. Unrelated sections of older full-config secrets are ignored. Dedicated secrets always supply database/CA; profiles always supply runner artifact/state/cache paths. No additional configuration secrets are required. Maintenance defaults use a 1 GiB cache/four connections; export uses one connection.

Profiles use `${RUNNER_TEMP}/manzara`; setup expands only `${RUNNER_TEMP}` in storage paths. CA secrets replace `sslrootcert` while preserving other TLS options; backup requires the CA, while maintenance/export can retain URL TLS settings. Config/certificates use mode 0600 and are removed at workflow exit. Google OAuth provisioning is separate. Keep backup credentials separate from document credentials; private YAML/base64 files belong under artifact `private/credentials/` with restrictive permissions. Base64 is not encryption:

```bash
umask 077
base64 -w 0 < ~/.manzara/private/credentials/maintenance/actions.yaml > ~/.manzara/private/credentials/maintenance/actions.base64
```

## Local storage

YAML `artifacts_root` contains `cache/`, `workspaces/`, historical `logs/`, `state/`, `durable/`, and `private/`. `app/artifacts.py` writes `STORAGE_LAYOUT.txt` with removal guidance. The MD5-verified source cache uses `documents.cache_path`, bounded by `documents.cache_max_gib`.

Tasks use redacted stdout, never log files/events. JSON artifacts at `workspaces/task-runs/<task_id>/run-<run_id>/artifact-<sequence>.json` are linked in local summaries; detailed flow outputs use dedicated workspaces. Existing files remain. Remove SQLite only with all Manzara processes stopped; it is never reconstructed from PostgreSQL. CLI controls: [README](../README.md#controls-and-lifecycle).

## Scheduled sync and cleanup

[daily-sync-cleanup.yml](../.github/workflows/daily-sync-cleanup.yml) runs at 09:37 UTC / 12:37 Moscow or manual dispatch. `python scripts/run_daily_maintenance.py` prepares cleanup, then executes persisted cleanup and Yandex Sync, sequentially without candidate limits. Preparation failure/stop/deferral prevents Sync. ISBN groups need explicit [review decisions](document-cleanup.md); newly discovered documents join the next preparation cohort.

The shared batch runtime owns local session locking/recovery, read-only preflight, signals, status snapshots, and artifacts. SIGINT/SIGTERM or `maintenance.daily_budget_seconds` request safe stop; failed/stopped/deferred/unfinalized work returns nonzero. The six-hour Actions limit can interrupt finalization. Durable cleanup phases resume; uncommitted discovery buffers rebuild. No automatic migrations run.

## Backblaze document transfer

[backblaze-document-transfer.yml](../.github/workflows/backblaze-document-transfer.yml) follows successful same-repository default-branch `Yadisk sync`, including empty discovery runs, or manual default-branch dispatch. Both execute trusted default-branch code; the workflow must be deployed there for chaining.

`scripts/run_backblaze_transfer.py` is an internal Actions runner, absent from CLI selectors. It uses the same maintenance profile/runtime and `maintenance.transfer_budget_seconds` within a six-hour job. It reads missing/incomplete PostgreSQL primary checkpoints directly, verifies B2 before download, processes one document at a time, and removes owned temporary bytes. Complete checkpoints are skipped; invalid/protected/privacy-inconsistent checkpoints need review. [Storage guidance](../app/modules/maintenance/guidance/storage.md) owns integrity, recovery, and cache details.

Independent item failures continue but leave unresolved work and return nonzero; busy documents defer. Empty work succeeds. Confirmed uploads with failed commits can recover from deterministic objects without redownload when identity/size evidence suffices. Sync needs at least three pool connections, transfer two; profiles supply the bound. Shared document locks coordinate conflicting operations across machines.

## Maintenance workflow diagnostics

Daily maintenance and transfer have separate non-canceling concurrency groups, install their minimal dependencies from `requirements.txt`, and start with fresh local orchestration. PostgreSQL remains checkpoint truth; never restore SQLite from Actions artifacts/caches.

Shared stdout logs flush immediately. `runtime.status_interval_seconds` emits run/elapsed/last-log/progress/provider-wait snapshots; advancing counters establish progress. GitHub summaries report outcomes/counters/job status. JSON results and SQLite diagnostics have seven-day retention, including available failure files; setup failures may have none. Credentials, document bytes, and caches are excluded. Final steps remove private config; transfer also removes its temporary workspace. Credential-backed runs require explicit owner authorization; [verification](verification.md) defines inspection limits.

## Scheduled Google export

[nightly-google-export.yml](../.github/workflows/nightly-google-export.yml) runs at 00:07 UTC or manual dispatch using `python -m app.modules.maintenance.runtime.dump_state --validate-sharing`. YAML `google.sheets` selects the target; the former `tt` worksheet is renamed in place. OAuth token JSON lives at artifact `private/credentials/google-drive/personal_token.json` with Sheets refresh authorization; no Drive API scope/archive is used.

`app/catalog/export.py` reads normalized relations in bounded batches within one read-only repeatable-read snapshot. The sharing gate uses that snapshot: restricted-folder descendants require `sharing_restricted=true`, `enc:` links when present, and blank `ya_public_url`. Errors report MD5/rules without links; no decryption occurs. Prepare all cells and validate lengths before RAW worksheet replacement.

Columns: `md5`, `mime_type`, `ya_path`, `ya_public_url`, `publisher`, `author`, `title`, `isbn`, `publish_year`, `language`, `translated`, `page_count`, `full`, `sharing_restricted`, `document_url`, `content_url`, `meta`, `size`. `meta` is generated Schema.org JSON; `size` is persisted primary S3 bytes in binary units, blank if unknown. Action summaries report counts/failure; check deployed run history for execution evidence.

## Local runtime state

The approved fresh-state policy resets older SQLite databases to schema version 8: local history, Gemini coordination, retry exclusions, attempts/errors, and SQLite caches are cleared on first initialization. PostgreSQL checkpoints, artifact files, and source caches are unaffected. Current-version startups retain state; unknown future versions fail. Task definitions remain Python-owned; run options/progress/provider waits/artifact references are local. `panel_id` denotes the task group.

## Database tools

`PYTHONPATH=. .venv/bin/alembic heads` inspects the code chain; `current` connects to the selected database. Startup never migrates PostgreSQL.

The single parentless baseline is `20261008_0062`, frozen in `alembic/sql/baseline_0062.sql`, including durable relations/views/functions/triggers/constraints and `pg_trgm` in `public`. Bootstrap requires PostgreSQL 18+, UTF-8, an empty catalog schema, and no existing `public.document`, `public.metadata`, `public.classification`, or `public.isbn_keep_many`. Configure URL/schema, then separately run `PYTHONPATH=. .venv/bin/alembic upgrade head`. Bootstrap is transactional, refuses domain objects, and creates no corpus/local rows. The role needs schema/extension creation rights, or an administrator must preinstall `pg_trgm` in `public`.

Databases already at this revision retain data; do not restamp. Future migrations descend from it. Fresh bootstrap omits migration-only adapter installers; deployed databases need no function cleanup.

Below `0062`, use commit `c9782d1` and its original chain before switching checkouts. Historical `scripts/reduce_postgres_storage.py --apply` supplies the final verified local transfer/recovery receipt; upgrade cannot bypass it. Never stamp older/populated unversioned schemas as baseline. Older restores require historical code and a reviewed upgrade plan.

Existing cutover artifacts remain at `durable/postgres-storage-cutover/<timestamp>/`, with hashes/paths in PostgreSQL evidence. Archive listing is not a restore drill. Historical import/cutover/manual helpers are absent; retrieve corresponding code only for planned recovery after reviewing schema/target assumptions. Pause writers, preserve manifests/index definitions, and verify restoration before restart. Never infer commit state from a client response or use automatic store fallback. See [backup/recovery](postgres-backup-recovery.md).

## Scheduled README link checker

[check_links.yml](../.github/workflows/check_links.yml) runs at 15:52 UTC. It uses the `link-checker` profile's timeout; schedule and broken-link branch behavior remain workflow-owned.
