# manzara

Manzara is a cloud-console style operations dashboard for Tatar-content workflows.

## Name Meaning

Tatar:
- `Манзара: Билгеле ноктадан күз алдында ачыла торган панорама, табигать күренеше, пейзаж`.

English:
- `Manzara: A panorama or landscape view that opens before the eyes from a specific point`.

Current architecture:
- FastAPI backend
- PostgreSQL state store (tasks, runs, logs, events, workflows, schedules)
- Schema management via Alembic migrations (no runtime DDL bootstrap)
- Modular flows in one monorepo (`shayan`, `maintenance`, `library`)
- Live updates via SSE (`/api/events/stream`)
- S3-compatible primary storage for documents, with Yandex Disk retained as an auxiliary source

Transitional note:
- Shayan flow now keeps persistent state in PostgreSQL (`shayan_manifest_entries`, `shayan_snapshots`, `shayan_snapshot_entries`).
- Legacy `~/.manzara/shayan/status.json` and `~/.manzara/shayan/snapshots/latest.json` are used only for one-time migration when DB state is empty; they are not runtime source of truth after cutover.

## UI Reference

Frontend visual direction is inspired by:
- https://github.com/builderz-labs/mission-control

Manzara is an independent implementation tailored to this repository's workflow model and APIs.

Current UI foundations:
- Shared responsive console shell with expandable navigation
- Command palette for pages, flows, and tasks (`Ctrl/Cmd+K` or `/`)
- API bootstrap followed by SSE-driven operational updates
- Shared custom dialogs/toasts; browser system dialogs are not used
- Shared tail/follow/backfill log viewer presented as a desktop drawer or mobile full-screen view
- Pinned local Lucide icon runtime (no unversioned CDN dependency)

## Current Product Scope

Pages:
- `/database`
- `/gemini`
- `/schedules`
- `/tasks`
- `/tasks/{task-slug-or-id}`
- `/flows/{flow-slug-or-id}`
- `/library`
- `/library/classifications`
- `/library/classifications/{classification_id}`
- `/library/personalities`
- `/library/publishers`
- `/library/collections`
- `/library/normalization/personality`
- `/library/normalization/publisher`

Routing note:
- `/` redirects to `/tasks`
- `/dashboard` redirects to `/tasks` (dashboard page is currently disabled)

Flow tasks (seeded at startup):
- `shayan.scan_changes`
- `shayan.download_new`
- `shayan.upload_yadisk`
- `shayan.transfer_yadisk_webdav`
- `maintenance.pgbackrest_backup_full`
- `maintenance.pgbackrest_backup_incr`
- `maintenance.sync_documents_s3`
- `maintenance.monocorpus_meta_evaluate`
- `library.collection_detect`
- `library.collection_apply`
- `library.generate_book_previews`
- `library.personality_suggestions_refresh`
- `library.publisher_suggestions_refresh`

Workflows (seeded at startup):
- `shayan.weekly_sync` (scan -> conditional download)
- `maintenance.pgbackrest_full_weekly`
- `maintenance.pgbackrest_incr_3h`
- `library.meta_evaluate`
- `library.personality_normalization_refresh`
- `library.publisher_normalization_refresh`

Scheduler policy:
- Weekly and interval schedule types
- Overlap policy: `skip`
- Catch-up policy on downtime: `once`
- Timezone field is stored per schedule (default `UTC`)

Schedule update contract (`PATCH /api/schedules/{schedule_id}`):
- `enabled` accepts:
  - booleans (`true` / `false`)
  - numeric `0` or `1`
  - strings: `1,true,yes,on` and `0,false,no,off`
- `day_of_week` must be an integer `1..7`
- `interval_minutes` must be an integer `>= 1` (or `null`/empty when not used)
- `timezone` must be a valid IANA timezone name (for example `UTC`, `Europe/Moscow`, `America/Los_Angeles`)

Runtime control behavior:
- Task toggle: `start -> graceful stop -> force stop`
- Header stop-all button: first press graceful, second press force
- Run logs stream into DB and are visible in UI
- High-frequency `task.log` SSE events do not reload page datasets; relevant lifecycle/artifact events use targeted, coalesced reconciliation.
- Each run also writes a dedicated artifact log file under `~/.manzara/task_runs/<task_id>/run-<run_id>.log` (or `MANZARA_ARTIFACTS_ROOT/task_runs/...` when overridden)
- Task and flow pages render run history with backend-provided structured summaries (`runs.summary_json`)
- Shayan scan/download run summaries include structured task artifacts (for example scan added/changed/removed counts) in `runs.summary_json.artifacts`.
- Shayan Yandex upload keeps resumable state in `shayan_manifest_entries` (`yadisk_status`, `yadisk_uploaded_payload_hash`, `yadisk_remote_path`, `yadisk_last_error`, timestamps).
- Shayan Yandex-to-Nextcloud transfer checkpoints each video in `shayan_webdav_transfers`. It uses Nextcloud chunked upload v2, assembles into deterministic temporary DAV paths, and independently verifies content before the final DAV move. Verified rows remain `uploaded`, making subsequent runs skip them without uploading again. The task emits chunk-level byte progress over SSE, stops gracefully at file boundaries, and restarts only an interrupted current chunk upload. It never deletes, trashes, or moves source videos on Yandex Disk.
- Long-running tasks can persist `runs.progress_json`; `task.progress` SSE events update determinate progress bars without frontend-owned domain state.
- Document synchronization keeps Yandex Disk as a non-destructive auxiliary source while S3 is primary. It verifies existing objects, copies missing/corrupt objects, checkpoints verification on `document`, and reports live progress and structured run artifacts.

Library data tooling currently includes:
- Classification views and merge/normalization previews
- Personality and publisher views
- Resumable PDF preview generation for applicable Library books:
  - WebP variants bounded to `400x600` (quality 80) and `1000x1500` (quality 85)
  - First page for one-page PDFs, first/last for two-page PDFs, and first/second/last otherwise
  - PostgreSQL manifests, per-object S3 verification, live progress, and per-run structured summaries
- Normalization workbench:
  - Review queue
  - Canonical registry
  - Suggestions refresh (heuristics + optional Gemini)
  - Bulk link/reject
  - Merge candidates and merge action
  - Audit history with undo
  - Evidence samples

## Requirements

- Python 3.10+
- Access to local repositories:
  - Shayan downloader repo (default: `/home/tans1q/projects/shayan-video-downloader`)
  - Monocorpus repo (default: `/home/tans1q/projects/monocorpus`)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Dependency policy:
- Keep a single dependency file: `requirements.txt`.
- If embedded runtime code adds a new external import, add it to `requirements.txt`.

### Library preview storage

Create the `ttbook-previews` bucket manually in Yandex Object Storage and configure anonymous read access. Manzara validates that authenticated access works but does not create the bucket or change its public policy.

Keep these bucket entries in the local unmasked configuration:

```yaml
yandex:
  cloud:
    bucket:
      document: ttdoc
      book_previews: ttbook-previews
```

The `library.generate_book_previews` task reuses and populates the persistent source cache at `~/.monocorpus/0_entry_point`. Temporary render files stay under `~/.manzara/library/book-previews`. Stop requests finish the current PDF, and the next run resumes missing variants from PostgreSQL and verified S3 objects.

### Primary document storage

Create `ttdoc-private` manually before the first sync and keep it private. Manzara validates access but does not create buckets or change bucket policies. `ttdoc` remains the public primary bucket; restricted documents are copied to `ttdoc-private` and read through short-lived backend-signed URLs.

```yaml
documents:
  cache_path: /home/tans1q/.monocorpus/0_entry_point

encryption_key: "<secret>"

yandex:
  disk:
    oauth_token: "<token>"
    documents:
      source_path: /neurotatarlar/kitaplar/monocorpus
      restricted_path: /neurotatarlar/kitaplar/monocorpus/__ТАРАТМАСКА_DONT_SHARE_НЕ_ДЕЛИТЬСЯ
  cloud:
    endpoint_url: https://storage.yandexcloud.net
    region_name: ru-central1
    aws_access_key_id: "<access-key>"
    aws_secret_access_key: "<secret-key>"
    bucket:
      document: ttdoc
      document_private: ttdoc-private
      upstream_metadata: upstream-metadata
```

`maintenance.sync_documents_s3` recursively discovers files only from the configured Yandex root. For required bytes it uses a hash-valid cache file first, an existing S3 location second, and Yandex Disk last. Cache-only files are ignored, and the document sync never writes into the cache.

Legacy S3 objects are verified rather than blindly replaced. Plain ETags and reproducible boto3 multipart ETags are checked first; otherwise the object is downloaded once and hashed. Only missing or mismatched objects are uploaded. Verification size, ETag, and timestamp are persisted on `document`, so unchanged objects are cheap on later runs.

Objects use flat content-addressed keys (`<md5>.<extension>`), independent of the Yandex folder hierarchy. Existing valid object keys are retained. Revision `20260731_0013` adds `primary_storage_size`, `primary_storage_etag`, and `primary_storage_verified_at` to the existing `document` table; normal application startup applies it automatically.

The task never deletes Yandex files. After a restricted file is safely copied to the private bucket and PostgreSQL is committed, its legacy public `ttdoc` object is deleted and absence is verified. Stop requests finish the current document; per-file failures are logged and processing continues, with a failed final run when any item failed. The task has no default schedule: run it manually from the Maintenance flow or explicitly configure a schedule on `/schedules`.

The first run can be I/O-intensive because unverifiable legacy S3 objects may need one download for a content hash. Later runs reuse PostgreSQL verification checkpoints when object size and ETag are unchanged.

## Run

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload --timeout-graceful-shutdown 10
```

On startup, Manzara applies pending Alembic migrations to `MANZARA_DB_SCHEMA` before seeding task/workflow definitions.

## Configuration

Environment variables:
- `MANZARA_DATABASE_URL` (PostgreSQL URL; required unless available in local YAML config)
- `MANZARA_DB_SCHEMA` (default: `monocorpus`)
- `MANZARA_ENABLE_SCHEDULER` (default: `1`; set `0` to disable scheduler)
- `MANZARA_CONFIG_PATH` (optional explicit YAML config path for embedded runtimes)
- `MANZARA_ARTIFACTS_ROOT` (default: `~/.manzara`; shared artifact root)
- `SHAYAN_REPO_PATH` (default: `/home/tans1q/projects/shayan-video-downloader`)
- `SHAYAN_OUTPUT_PATH` (default: `~/.manzara/shayan`)
- `SHAYAN_YADISK_OAUTH_TOKEN` (optional override; defaults to `yandex.disk.oauth_token` in YAML)
- `SHAYAN_YADISK_CARTOONS_TARGET_DIR` (optional override; defaults to `yandex.disk.shayan.cartoons` in YAML)
- `SHAYAN_YADISK_SHOWS_TARGET_DIR` (optional override; defaults to `yandex.disk.shayan.shows` in YAML)
- `MONOCORPUS_REPO_PATH` (default: `/home/tans1q/projects/monocorpus`)
- `PG_BACKREST_STANZA` (default: `monocorpus`)
- `PG_BACKREST_S3_BUCKET` (default: `tt-monocorpus-postgres-backups`; used for S3 backup verification)
- `PG_BACKREST_S3_ENDPOINT` (default: `https://storage.yandexcloud.net`)

YAML configuration for Shayan Yandex upload and Nextcloud archival tasks:

```yaml
yandex:
  disk:
    oauth_token: "<token>"
    shayan:
      cartoons: "/neurotatarlar/video/shayantv/cartoons"
      shows: "/neurotatarlar/video/shayantv/shows"

nextcloud:
  webdav_url: "https://nx104082.your-storageshare.de/remote.php/dav/files/Admin"
  username: "Admin"
  password: "<password-or-app-password>"
  shayan:
    target_dir: "/Manzara/Shayan"
```

The existing account password works in `nextcloud.password`. A revocable app password created under Nextcloud Personal settings -> Security is preferable when available. Never commit either credential. The archival task preserves paths as `/Manzara/Shayan/<category>/<program>/<season>/<file>`.

The task sends 64 MiB chunks through Nextcloud's v2 upload endpoint, asks Nextcloud to assemble them into a deterministic staging path, and then streams the staged file back to verify its MD5 before the final WebDAV `MOVE`. A stored `OC-Checksum` value alone is never accepted as upload proof. The Yandex source is retained unchanged after successful verification.

Embedded runtimes read YAML config in this order:
1. `MANZARA_CONFIG_PATH` (if set)
2. `./config.local.yaml`
3. `./config.yaml`

Secrets policy:
- `config.yaml` and `config.local.yaml` are local-only (gitignored).
- Keep `config.example.yaml` masked and in sync with real config structure.

Gemini config (preferred shape):

```yaml
gemini:
  models:
    library_meta_evaluate: "gemini-3-flash-preview"
    library_normalization: "gemini-2.5-flash"
  accounts:
    account_a:
      - "AIza..."
      - "AIza..."
    account_b:
      - "AIza..."
```

Legacy `gemini_api_keys: []` is still supported as fallback (single default account).

Model policy:
- Task flows should resolve Gemini model names from `gemini.models` aliases (not hardcoded in task logic).
- Current default aliases are:
  - `library_meta_evaluate`
  - `library_normalization`

Backup task note:
- Maintenance backup tasks use `sudo -n -u postgres pgbackrest ...`.
- Manual and scheduled runs are non-interactive. If sudo access is not configured, backup tasks fail.
- Success validation is S3-based:
  - capture S3 backup-label snapshot before run
  - wait for a new label after run (default poll window: up to 120 seconds)
  - verify required files for the new label exist in S3
- Configure passwordless sudo for backup commands:

```bash
PG=$(command -v pgbackrest)
printf 'tans1q ALL=(postgres) NOPASSWD: %s --stanza=monocorpus --type=full backup\n' "$PG" | sudo tee /etc/sudoers.d/manzara-pgbackrest >/dev/null
printf 'tans1q ALL=(postgres) NOPASSWD: %s --stanza=monocorpus --type=incr backup\n' "$PG" | sudo tee -a /etc/sudoers.d/manzara-pgbackrest >/dev/null
sudo chmod 440 /etc/sudoers.d/manzara-pgbackrest
sudo visudo -cf /etc/sudoers.d/manzara-pgbackrest
```

- Quick verification:

```bash
sudo -n -u postgres pgbackrest --stanza=monocorpus --type=full backup
sudo -n -u postgres pgbackrest --stanza=monocorpus --type=incr backup
```

- Database state page permissions:
  - Core table/size/backup data works without elevated PostgreSQL roles.
  - Disk path/free-space metrics require reading `SHOW data_directory`, which needs `pg_read_all_settings`.
  - Grant:

```bash
sudo -u postgres psql -d postgres -c "GRANT pg_read_all_settings TO tans1q;"
```

  - Revoke (optional):

```bash
sudo -u postgres psql -d postgres -c "REVOKE pg_read_all_settings FROM tans1q;"
```

- To verify backup files were uploaded to S3 for a run label, use:
  - `.venv/bin/python app/modules/maintenance/runtime/check_backup_s3.py --task-id maintenance.pgbackrest_backup_incr`
  - `.venv/bin/python app/modules/maintenance/runtime/check_backup_s3.py --task-id maintenance.pgbackrest_backup_full`

## Useful Runtime Commands

Inspect artifact run logs:

```bash
ls -lah ~/.manzara/task_runs
tail -f ~/.manzara/task_runs/<task_id>/run-<run_id>.log
```

Artifact log line standard:

```text
<ISO8601-UTC timestamp> | <LEVEL> | run_id=<id> task_id=<task_id> panel_id=<panel_id> source=<runtime|stdout> | <message>
```

Observability notes:
- DB run logs (`/api/runs/{run_id}/logs`) remain the UI/SSE source.
- Artifact run logs are durable per-run files for offline auditing and long-task troubleshooting.
- Stream reader failures now emit explicit `log_stream_error=...` lines (DB + SSE + artifact log) instead of failing silently.

Manual normalization suggestion refresh:

```bash
.venv/bin/python app/modules/library/runtime/run_normalization_refresh.py --entity-type personality --limit 180
.venv/bin/python app/modules/library/runtime/run_normalization_refresh.py --entity-type publisher --limit 180
```

Disable Gemini suggestions for refresh:

```bash
.venv/bin/python app/modules/library/runtime/run_normalization_refresh.py --entity-type personality --limit 180 --no-gemini
```

## Tests

Run test suite:

```bash
.venv/bin/python -m pytest -q
```

Run frontend behavior tests:

```bash
node --test tests/frontend/*.mjs
```

Coverage notes:
- API/scheduler/task-control behavior is covered by `pytest`.
- Backend runtime logging tests include secret redaction regression checks (including `Authorization: Bearer ...` and secret query params) and stream error visibility checks.
- Shared frontend helpers, shell contracts, and page behavior are covered by `node:test` (`tests/frontend/*.mjs`, including schedules, tasks, task/flow detail, library pages, database, Gemini, and normalization pages).
- Normalization interaction coverage includes queue pagination, stop-all force-confirmation guard, suggestions refresh payload checks, bulk queue actions, suggestion accept/reject, merge, history undo calls, cross-tab queue-open transitions, and evidence dialog fetch/render checks.
- Runtime-heavy external flows still require manual smoke checks, especially:
  - `maintenance.monocorpus_meta_evaluate`
  - normalization refresh with real config + Gemini keys
  - `library.generate_book_previews` against a real PDF source and preview bucket
  - `shayan.transfer_yadisk_webdav` against the real Nextcloud account:
    - confirm password authentication and target quota before transfer
    - confirm chunk-level byte progress reaches the task card through SSE
    - confirm a video reaches the expected hierarchy, is hash-verified, and remains on Yandex Disk
    - stop after one file and confirm the next run resumes without uploading the verified file again
  - `maintenance.sync_documents_s3` against real Yandex/S3 services:
    - confirm a cached document uploads without a Yandex download
    - confirm an unchanged verified object is skipped on the next run
    - confirm a restricted document is private, its stored URL is encrypted, and any legacy public copy is absent
    - request graceful stop and confirm the current document finishes before the run stops

## API Summary

Core:
- `GET /api/health`
- `GET /api/system/state`
- `GET /api/dashboard`
- `GET /api/schedules`
- `GET /api/tasks`
- `GET /api/tasks/{task_id_or_slug}`
- `GET /api/flows/{flow_id_or_slug}`
- `GET /api/database/state`
- `GET /api/gemini/state`
- `POST /api/tasks/{task_id}/toggle`
- `PATCH /api/tasks/{task_id}/title`
- `PATCH /api/flows/{panel_id}/title`
- `POST /api/workflows/{workflow_id}/run`
- `GET /api/workflows/{workflow_id}`
- `PATCH /api/schedules/{schedule_id}`
- `POST /api/system/stop-all`
- `POST /api/gemini/reset-key`
- `POST /api/gemini/reset-all`
- `GET /api/runs/{run_id}/logs`
- `GET /api/events/stream`

Library:
- `GET /api/library`
- `GET /api/library/previews/{md5}`
- `GET /api/library/classifications`
- `GET /api/library/classifications/insights`
- `GET /api/library/classifications/normalization-preview`
- `GET /api/library/classifications/merge-candidates`
- `GET /api/library/classifications/{classification_id}`
- `GET /api/library/personalities`
- `GET /api/library/personalities/table`
- `GET /api/library/personalities/insights`
- `GET /api/library/publishers`
- `GET /api/library/publishers/table`
- `GET /api/library/publishers/insights`
- `GET /api/library/collections`
- `GET /api/library/collections/table`
- `GET /api/library/collections/insights`
- `GET /api/library/collections/{collection_id}/items`
- `PATCH /api/library/collections/{collection_id}`

Normalization API (`{entity_type}` = `personality|publisher`):
- `GET /api/library/normalization/{entity_type}`
- `GET /api/library/normalization/{entity_type}/queue`
- `GET /api/library/normalization/{entity_type}/canonicals`
- `POST /api/library/normalization/{entity_type}/canonicals`
- `POST /api/library/normalization/{entity_type}/decisions/link`
- `POST /api/library/normalization/{entity_type}/decisions/create-link`
- `POST /api/library/normalization/{entity_type}/decisions/reject`
- `POST /api/library/normalization/{entity_type}/bulk/link`
- `POST /api/library/normalization/{entity_type}/bulk/reject`
- `GET /api/library/normalization/{entity_type}/suggestions`
- `POST /api/library/normalization/{entity_type}/suggestions/refresh`
- `GET /api/library/normalization/{entity_type}/merge-candidates`
- `POST /api/library/normalization/{entity_type}/merge`
- `GET /api/library/normalization/{entity_type}/history`
- `POST /api/library/normalization/{entity_type}/history/{event_id}/undo`
- `GET /api/library/normalization/{entity_type}/quality`
- `GET /api/library/normalization/{entity_type}/evidence`
