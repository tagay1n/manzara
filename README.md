# manzara

Manzara is a cloud-console style operations dashboard for Tatar-content workflows.

## Name Meaning

Tatar:
- `Манзара: Билгеле ноктадан күз алдында ачыла торган панорама, табигать күренеше, пейзаж`.

English:
- `Manzara: A panorama or landscape view that opens before the eyes from a specific point`.

Current architecture:
- FastAPI backend
- PostgreSQL state store (tasks, runs, logs, events, and conveyor state)
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
- API bootstrap followed by SSE-driven operational updates
- Shared custom dialogs/toasts; browser system dialogs are not used
- Shared tail/follow/backfill log viewer presented as a desktop drawer or mobile full-screen view
- Pinned local Lucide icon runtime (no unversioned CDN dependency)

## Current Product Scope

Pages:
- `/database`
- `/gemini` (masked key/runtime state, reset controls, and an expiring confirmed override for the active reset blackout)
- `/tasks`
- `/tasks/{task-slug-or-id}`
- `/library`
- `/library/classifications`
- `/library/classifications/{classification_id}`
- `/library/personalities`
- `/library/publishers`
- `/library/collections`
- `/library/document-cleanup`
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
- `maintenance.monocorpus_sync`
- `maintenance.monocorpus_meta_evaluate`
- `library.collection_detect`
- `library.collection_validate`
- `library.collection_apply`
- `library.generate_book_previews`
- `library.metadata_extract`
- `library.prepare_document_cleanup`
- `library.personality_suggestions_refresh`
- `library.publisher_suggestions_refresh`

Runtime control behavior:
- Task toggle: `start -> graceful stop -> force stop`
- Header stop-all button: first press graceful, second press force
- Run logs stream into DB and are visible in UI
- High-frequency `task.log` SSE events do not reload page datasets; relevant lifecycle/artifact events use targeted, coalesced reconciliation.
- Each run also writes a dedicated artifact log file under `~/.manzara/task_runs/<task_id>/run-<run_id>.log` (or `MANZARA_ARTIFACTS_ROOT/task_runs/...` when overridden)
- Task pages render run history with backend-provided structured summaries (`runs.summary_json`)
- Shayan scan/download run summaries include structured task artifacts (for example scan added/changed/removed counts) in `runs.summary_json.artifacts`.
- Shayan Yandex upload keeps resumable state in `shayan_manifest_entries` (`yadisk_status`, `yadisk_uploaded_payload_hash`, `yadisk_remote_path`, `yadisk_last_error`, timestamps).
- Shayan Yandex-to-Nextcloud transfer checkpoints each video in `shayan_webdav_transfers`. It uses Nextcloud chunked upload v2, assembles into deterministic temporary DAV paths, and independently verifies content before the final DAV move. Verified rows remain `uploaded`, making subsequent runs skip them without uploading again. The task emits chunk-level byte progress over SSE, stops gracefully at file boundaries, and restarts only an interrupted current chunk upload. It never deletes, trashes, or moves source videos on Yandex Disk.
- Long-running tasks can persist `runs.progress_json`; `task.progress` SSE events update determinate progress bars without frontend-owned domain state.
- Document storage upload uses PostgreSQL's null-`document_url` rows as its queue, reuses verified cache entries, downloads cache misses by persisted Yandex path, verifies Backblaze objects, and checkpoints storage state on `document` with live progress and structured artifacts.
- Document cleanup is split into preparation and execution. `library.prepare_document_cleanup` only writes PostgreSQL plans/reviews. `maintenance.monocorpus_sync` applies persisted plans, synchronizes Yandex catalog entries, publishes missing links only for unrestricted documents, and records duplicate-MD5 removals before executing them.

Library data tooling currently includes:
- Classification views and merge/normalization previews
- Personality and publisher views
- Path-independent collection workflow:
  - Operational tasks are grouped in the dedicated **Collections** catalog on `/tasks`; the Library collections review page remains at `/library/collections`.
  - **Discover collections** indexes eligible `metadata.schema_org` records and writes proposals without mutating approved memberships.
  - Documents require a usable metadata title; `Legislation` and normalized legal-document genres from `LEGAL_GENRE_BLACKLIST` are excluded before clustering.
  - **Validate collection proposals** uses an adaptive Gemini model pool with strict per-MD5 JSON responses and resumable PostgreSQL attempts.
  - Review supports per-document selection. Approval creates authoritative membership; applying collection metadata remains a separate task.
- Resumable PDF preview generation for applicable Library books:
  - WebP variants bounded to `400x600` (quality 80) and `1000x1500` (quality 85)
  - First page for one-page PDFs, first/last for two-page PDFs, and first/second/last otherwise
  - PostgreSQL manifests, per-object S3 verification, live progress, and per-run structured summaries
- Resumable Schema.org metadata extraction:
  - Selects only documents with a verified Backblaze primary-storage checkpoint
  - Uses extracted text when present; otherwise reads PDF bytes only from Backblaze
  - Preserves the established monocorpus prompt, PDF edge-page slicing, and normalization
  - Tries the configured model pool in order and checkpoints content failures in PostgreSQL
  - Writes metadata to PostgreSQL only; it does not write metadata ZIPs or mutate storage URLs
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

Create the public `ttpreviews` bucket manually in the same Backblaze region as the primary document buckets. Enable SSE-B2, leave Object Lock disabled, and grant the Manzara application key permission to list, read, write, and delete its objects. Manzara validates access but does not create the bucket or change its policy.

Keep these bucket entries in the local unmasked configuration:

```yaml
documents:
  primary_storage:
    bucket:
      public: ttdocs
      private: ttdocs-restricted
      book_previews: ttpreviews
```

The `library.generate_book_previews` task selects only unrestricted Library-applicable PDFs whose URL belongs to the public Backblaze document bucket. It reuses and populates the persistent source cache at `~/.monocorpus/0_entry_point`; render files are retained under `~/.manzara/library/book-previews` for inspection. Preview keys are deterministic (`<md5>/1s.webp`, `<md5>/1l.webp`, and the applicable second/last variants). PostgreSQL stores document-level status, page count, and recipe version rather than duplicating S3 object metadata. Stop requests finish the current PDF, and the next run verifies and reuses existing objects before generating missing variants.

### Rich non-PDF content

Create public Backblaze buckets configured as `documents.primary_storage.bucket.content` and `content_images`. The `library.extract_non_pdf` task writes `<md5>.zip` archives containing one `<md5>.md` file and stores referenced images as `<md5>/<ordinal>.<extension>`. Its catalog command processes every eligible non-PDF document and resumes from PostgreSQL checkpoints; untouched documents run before retries, operational failures receive at most three automatic attempts per extractor version, and deterministic failures are deferred until the extractor changes. The CLI retains `--per-mime-limit` for deliberate QA cohorts and `--retry-known-failures` for explicit retries. It backfills legacy content, reads sources only from the verified shared cache or primary Backblaze storage, validates that every uploaded image is represented in the final HTML figure markup, removes stale image objects after a successful checkpoint, and retains all inspection files under `~/.manzara/library/non-pdf-extraction`. Pandoc and headless LibreOffice (`soffice`) must be installed. Manzara validates the buckets but never creates them or changes their public policy.

### Primary document storage

Backblaze B2 is the primary document store. Create one public bucket and one private bucket in the same Backblaze region before the first sync; Manzara validates access but does not create buckets or change bucket policies. Create a dedicated application key, not the master key, with `listBuckets`, `readBuckets`, `listFiles`, `readFiles`, `writeFiles`, and `deleteFiles` capabilities and access to both buckets. `readBuckets` is required for the ACL safety check, `listFiles` covers unfinished multipart uploads, and `deleteFiles` removes obsolete public copies of restricted documents. Copy the exact S3 endpoint and region shown by Backblaze.

```yaml
documents:
  cache_path: /home/tans1q/.monocorpus/0_entry_point
  primary_storage:
    endpoint_url: https://s3.eu-central-003.backblazeb2.com
    region_name: eu-central-003
    access_key_id: "<backblaze-application-key-id>"
    secret_access_key: "<backblaze-application-key>"
    bucket:
      public: manzara-documents
      private: manzara-documents-private

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

`documents.primary_storage` is isolated from `yandex.cloud`: changing the document primary does not repoint upstream metadata, backups, or unrelated Yandex Object Storage consumers. Backblaze clients use SigV4 and path-style addressing. Library metadata evaluation reads signed private documents from Backblaze; preview generation reads source PDFs from Backblaze and writes generated previews to the configured public Backblaze preview bucket.

`library.metadata_extract` requires a verified Backblaze checkpoint (`document_url`, size, and verification timestamp) before a document is eligible. It reuses MD5-verified source documents from the shared cache and populates cache misses only from Backblaze; it never falls back to Yandex Disk or legacy S3. A source read failure remains retryable. Metadata is accepted only when it contains a usable title and at least one independent bibliographic/content signal. Boilerplate-only, title-only, and title-missing responses advance to the next configured model. Existing usable metadata is immutable; low-quality historical metadata is replaced only after a validated better response and remains preserved if every model fails. Content-level failures are checkpointed per model in `library_metadata_extraction_state`; after every configured model fails, the document remains excluded until its state row is manually removed. If only the models remaining for one document have exhausted keys, that document is deferred while the batch continues; the task stops for quota only when every configured model is unavailable. Progress and run summaries distinguish quota, service, and source deferrals from terminal documents and show the unresolved remaining count:

```sql
DELETE FROM monocorpus.library_metadata_extraction_state
WHERE md5 = '<document-md5>';
```

`maintenance.monocorpus_sync` recursively traverses the configured Yandex Disk root, applies persisted cleanup plans, handles duplicate resources, publishes missing links for ordinary unrestricted documents, and inserts or updates Yandex catalog fields in PostgreSQL. It never downloads document bytes or uploads them to Backblaze. Cleanup execution may still delete managed S3 derivatives after the guarded Yandex action is verified.

`maintenance.sync_documents_s3`, displayed as **Upload to Backblaze S3**, reads only PostgreSQL rows whose `document_url` is null or blank. It never lists Yandex directories, discovers or inserts documents, or publishes Yandex links. A hash-valid shared cache file is reused first; on a cache miss, the task downloads the exact persisted `ya_path` from Yandex and verifies its MD5. An unavailable Yandex download is logged and skipped, leaving the row pending for a later run. The task never downloads source bytes from Backblaze or legacy S3.

If the expected content-addressed Backblaze object already exists, matching size plus `source-md5` metadata or a plain MD5 ETag allows the task to commit the checkpoint without re-uploading. A new upload is not downloaded again: a post-upload `HEAD` must confirm its expected size and submitted `source-md5` metadata before PostgreSQL is updated. Restricted-object cleanup also finishes before the Backblaze link is committed. The task updates only `document_url`, `primary_storage_size`, `primary_storage_etag`, and `primary_storage_verified_at` on the still-pending row.

`Sync` and **Upload to Backblaze S3** may run concurrently. Each upload checkpoint also requires the row's Yandex path, MIME type, and restriction state to match the snapshot used for the upload. If `Sync` changes or deletes that row first, a newly uploaded object from the stale attempt is removed and the checkpoint is skipped. Catalog changes that affect storage identity clear the existing storage checkpoint so the next upload run places and verifies the object using the current catalog state.

Objects use flat content-addressed keys (`<md5>.<extension>`), independent of the Yandex folder hierarchy. Existing valid object keys are retained. Revision `20260731_0013` adds `primary_storage_size`, `primary_storage_etag`, and `primary_storage_verified_at` to the existing `document` table; normal application startup applies it automatically.

The upload task never publishes, deletes, trashes, moves, or traverses Yandex Disk files. After a restricted file is safely copied to the Backblaze private bucket, an obsolete public S3 copy is deleted and absence is verified before PostgreSQL stores the private link. Boto3 callbacks emit byte-level SSE progress. Stop requests finish the current document; rows without a committed link remain pending. Per-item skips and failures are logged, processing continues, and the run completes with a visible queue report. Setup, authentication, configuration, duplicate-identity, and database-wide errors remain fatal.

Every finished upload emits a structured queue artifact used by the web run summary: pending before/after, processed, uploaded, recovered existing objects, cache/Yandex sources, skipped downloads, failures, bytes uploaded, and stop state.

Backblaze references: [S3-compatible endpoint and supported calls](https://www.backblaze.com/docs/en/cloud-storage-call-the-s3-compatible-api), [application key capabilities](https://www.backblaze.com/docs/cloud-storage-s3-compatible-app-keys).

## Run

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload --timeout-graceful-shutdown 10
```

On startup, Manzara applies pending Alembic migrations to `MANZARA_DB_SCHEMA` before seeding panel and task definitions.

## Configuration

Environment variables:
- `MANZARA_DATABASE_URL` (PostgreSQL URL; required unless available in local YAML config)
- `MANZARA_DB_SCHEMA` (default: `monocorpus`)
- `MANZARA_CONFIG_PATH` (optional explicit YAML config path for embedded runtimes)
- `MANZARA_ARTIFACTS_ROOT` (default: `~/.manzara`; shared artifact root)
- `SHAYAN_REPO_PATH` (default: `/home/tans1q/projects/shayan-video-downloader`)
- `SHAYAN_OUTPUT_PATH` (default: `~/.manzara/shayan`)
- `SHAYAN_YADISK_OAUTH_TOKEN` (optional override; defaults to `yandex.disk.oauth_token` in YAML)
- `MONOCORPUS_REPO_PATH` (default: `/home/tans1q/projects/monocorpus`)
- `PG_BACKREST_STANZA` (default: `monocorpus`)
- `PG_BACKREST_S3_BUCKET` (default: `tt-monocorpus-postgres-backups`; used for S3 backup verification)
- `PG_BACKREST_S3_ENDPOINT` (default: `https://storage.yandexcloud.net`)

YAML configuration for direct Shayan uploads and Yandex-to-Hetzner migration:

```yaml
yandex:
  disk:
    oauth_token: "<token>"

nextcloud:
  webdav_url: "https://nx104082.your-storageshare.de/remote.php/dav/files/Admin"
  username: "Admin"
  password: "<password-or-app-password>"
  shayan:
    cartoons:
      source_dir: "/neurotatarlar/video/shayantv"
      target_dir: "/Безнең тәҗрибә/Мультфильмнар"
```

Use a revocable app password created under Nextcloud Personal settings -> Security in `nextcloud.password`; providers may reject the regular account password for WebDAV. Never commit either credential. The `Upload` task sends newly downloaded local files directly to the category's `target_dir`, preserving the hierarchy below `videos/<category>/`, and deletes a local file only after independent verification on Hetzner. The `Migrate to Hetzner` task uses `source_dir` to discover existing Yandex files, preserves the hierarchy relative to that source, and retains the Yandex source unchanged. Categories without a `target_dir` are not uploaded; migration additionally requires `source_dir`.

Both tasks run a logged WebDAV preflight. A `401` fails once with an actionable credential error; a `429` waits and retries the same request, honoring `Retry-After` or using an interruptible backoff. Read-only `PROPFIND` probes retry transient `500`/`502`/`503`/`504` responses. Uploads use 64 MiB chunks through Nextcloud's v2 endpoint, assemble into a short deterministic `.manzara-<md5>.uploading` staging path, and stream the staged file back to verify its MD5 before the final WebDAV `MOVE`. Do not change the staging suffix to `.part`: Hetzner Storage Share returns a server-side `500 TypeError` for those probes. A stored `OC-Checksum` value alone is never accepted as upload proof.

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
  model_pool:
    - "gemini-3.7-flash"
    - "gemini-3.6-flash"
    - "gemini-3.5-flash"
    - "gemini-3-flash-preview"
  accounts:
    account_a:
      - "AIza..."
      - "AIza..."
    account_b:
      - "AIza..."
```

Model policy:
- Every Gemini operation resolves models from the one ordered `gemini.model_pool`; model names are never hardcoded in task logic.
- The shared pool is mandatory and has no code default. Normalization uses its first model; extraction, evaluation, and collection validation can advance through the configured order according to their response and retry policies.
- Collection proposal validation load-balances one verdict per request across the shared pool. Timeout or malformed responses reduce that model's batch size; quota/service/request errors follow the shared Gemini runtime policy.
- Metadata extraction tries the shared pool in order. Empty, malformed, timed-out, or rejected content moves to the next model; quota exhaustion rotates keys without marking the document failed. A `5xx` response or transient transport reset shares one bounded retry budget; repeated transport/service failure defers only the current document and the batch continues.

Backup task note:
- Maintenance backup tasks use `sudo -n -u postgres pgbackrest ...`.
- Backup runs are non-interactive. If sudo access is not configured, backup tasks fail.
- Success validation is S3-based:
  - capture S3 backup-label snapshot before run
  - snapshot bounded `backup.info` repository markers before run
  - wait for either a new label or changed repository markers after run (default poll window: up to 120 seconds)
  - verify required files for the new or resumed label exist in S3
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
- API and task-control behavior is covered by `pytest`.
- Backend runtime logging tests include secret redaction regression checks (including `Authorization: Bearer ...` and secret query params) and stream error visibility checks.
- Shared frontend helpers, shell contracts, and page behavior are covered by `node:test` (`tests/frontend/*.mjs`, including task detail, library pages, database, Gemini, and normalization pages).
- Normalization interaction coverage includes queue pagination, stop-all force-confirmation guard, suggestions refresh payload checks, bulk queue actions, suggestion accept/reject, merge, history undo calls, cross-tab queue-open transitions, and evidence dialog fetch/render checks.
- Runtime-heavy external flows still require manual smoke checks, especially:
  - `maintenance.monocorpus_meta_evaluate`
  - normalization refresh with real config + Gemini keys
  - Library collection discovery and validation against the real catalog:
    - apply Alembic migration `20260806_0018` and confirm approved collections and memberships retain their IDs while legacy suggested rows become proposal/history state
    - run `library.collection_detect` and confirm the artifact/SSE summary reports scanned, eligible, excluded legal, attachment, and new-collection counts
    - inspect a proposal and confirm its evidence and Gemini prompt contain metadata only, with no Yandex path or parent-directory data
    - run `library.collection_validate` and confirm per-model batch sizes decrease after malformed/timeout responses, recover after three successes, and persist attempts across restart
    - stop validation and confirm the current Gemini request finishes while unvalidated proposal items remain queued for the next run
    - approve selected proposal items and confirm only that explicit action creates canonical memberships; rerun discovery and confirm approved memberships and rejected decisions remain unchanged
    - confirm proposal, progress, and final summary changes arrive through SSE without parsing task logs
  - `library.generate_book_previews` against a real PDF source and preview bucket
  - `shayan.transfer_yadisk_webdav` against the real Nextcloud account:
    - confirm password authentication and target quota before transfer
    - confirm chunk-level byte progress reaches the task card through SSE
    - confirm a video reaches the expected hierarchy, is hash-verified, and remains on Yandex Disk
    - stop after one file and confirm the next run resumes without uploading the verified file again
  - `maintenance.sync_documents_s3` against real Yandex/Backblaze services:
    - confirm a cached document uploads without a Yandex download
    - confirm a cache miss downloads only the persisted PostgreSQL `ya_path` and performs no Yandex traversal or publishing
    - confirm the uploaded Backblaze object passes the size and `source-md5` metadata `HEAD` check before PostgreSQL is updated, without a read-back download
    - confirm an existing verified Backblaze object is checkpointed without re-uploading
    - confirm an unavailable Yandex path is reported as skipped and remains pending
    - confirm a restricted document is private, its stored URL is encrypted, and any legacy public copy is absent
    - confirm byte-level upload progress reaches the task card through SSE
    - request graceful stop and confirm the current document finishes before the run stops
    - force-stop one multipart upload and confirm the next run aborts its unfinished parts before retrying that document

## API Summary

Core:
- `GET /api/health`
- `GET /api/system/state`
- `GET /api/dashboard`
- `GET /api/tasks`
- `GET /api/tasks/{task_id_or_slug}`
- `GET /api/database/state`
- `GET /api/gemini/state`
- `POST /api/tasks/{task_id}/toggle`
- `PATCH /api/tasks/{task_id}/title`
- `PATCH /api/flows/{panel_id}/title`
- `POST /api/system/stop-all`
- `POST /api/gemini/reset-key`
- `POST /api/gemini/reset-all`
- `POST /api/gemini/override-blackout`
- `GET /api/runs/{run_id}/logs`
- `GET /api/events/stream`

Library:
- `GET /api/library`
- `GET /api/library/previews/{md5}`
- `GET /api/library/documents/{md5}/open`
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
- `GET /api/library/collections/{collection_id}/review`
- `GET /api/library/collections/{collection_id}/items`
- `PATCH /api/library/collections/{collection_id}`
- `POST /api/library/collections/{collection_id}/merge`
- `GET /api/library/collection-proposals`
- `GET /api/library/collection-proposals/{proposal_id}`
- `POST /api/library/collection-proposals/{proposal_id}/decision`

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
