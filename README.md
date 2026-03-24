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

## UI Reference

Frontend visual direction is inspired by:
- https://github.com/builderz-labs/mission-control

Manzara is an independent implementation tailored to this repository's workflow model and APIs.

## Current Product Scope

Pages:
- `/dashboard`
- `/database`
- `/schedules`
- `/tasks`
- `/tasks/{task-slug-or-id}`
- `/library`
- `/library/classifications`
- `/library/classifications/{classification_id}`
- `/library/personalities`
- `/library/publishers`
- `/library/normalization/personality`
- `/library/normalization/publisher`

Flow tasks (seeded at startup):
- `shayan.scan_changes`
- `shayan.download_new`
- `maintenance.monocorpus_sync`
- `maintenance.pgbackrest_backup_full`
- `maintenance.pgbackrest_backup_incr`
- `maintenance.monocorpus_meta_evaluate`
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

Runtime control behavior:
- Task toggle: `start -> graceful stop -> force stop`
- Header stop-all button: first press graceful, second press force
- Run logs stream into DB and are visible in UI

Library data tooling currently includes:
- Classification views and merge/normalization previews
- Personality and publisher views
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
- `SHAYAN_REPO_PATH` (default: `/home/tans1q/projects/shayan-video-downloader`)
- `SHAYAN_OUTPUT_PATH` (default: `/home/tans1q/video-archive`)
- `MONOCORPUS_REPO_PATH` (default: `/home/tans1q/projects/monocorpus`)
- `PG_BACKREST_STANZA` (default: `monocorpus`)

Embedded runtimes read YAML config in this order:
1. `MANZARA_CONFIG_PATH` (if set)
2. `./config.local.yaml`
3. `./config.yaml`
4. `./config.example.yaml` (masked, reference only)

Secrets policy:
- `config.yaml` and `config.local.yaml` are local-only (gitignored).
- Keep `config.example.yaml` masked and in sync with real config structure.

Backup task note:
- Maintenance backup tasks use `sudo -n -u postgres pgbackrest ...`.
- Manual and scheduled runs are non-interactive. If sudo access is not configured, backup tasks fail.
- Configure passwordless sudo for backup commands:

```bash
PG=$(command -v pgbackrest)
printf 'tans1q ALL=(postgres) NOPASSWD: %s --stanza=monocorpus --type=full backup\n' "$PG" | sudo tee /etc/sudoers.d/manzara-pgbackrest >/dev/null
printf 'tans1q ALL=(postgres) NOPASSWD: %s --stanza=monocorpus --type=incr backup\n' "$PG" | sudo tee -a /etc/sudoers.d/manzara-pgbackrest >/dev/null
printf 'tans1q ALL=(postgres) NOPASSWD: %s --stanza=monocorpus info --output=json\n' "$PG" | sudo tee -a /etc/sudoers.d/manzara-pgbackrest >/dev/null
sudo chmod 440 /etc/sudoers.d/manzara-pgbackrest
sudo visudo -cf /etc/sudoers.d/manzara-pgbackrest
```

- Quick verification:

```bash
sudo -n -u postgres pgbackrest --stanza=monocorpus --type=full backup
sudo -n -u postgres pgbackrest --stanza=monocorpus --type=incr backup
sudo -n -u postgres pgbackrest --stanza=monocorpus info --output=json
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

Coverage notes:
- API/scheduler/task-control behavior is covered by `pytest`.
- Runtime-heavy external flows still require manual smoke checks, especially:
  - `maintenance.monocorpus_meta_evaluate`
  - normalization refresh with real config + Gemini keys

## API Summary

Core:
- `GET /api/health`
- `GET /api/dashboard`
- `GET /api/schedules`
- `GET /api/tasks`
- `GET /api/tasks/{task_id_or_slug}`
- `GET /api/database/state`
- `POST /api/tasks/{task_id}/toggle`
- `PATCH /api/tasks/{task_id}/title`
- `PATCH /api/flows/{panel_id}/title`
- `POST /api/workflows/{workflow_id}/run`
- `GET /api/workflows/{workflow_id}`
- `PATCH /api/schedules/{schedule_id}`
- `POST /api/system/stop-all`
- `GET /api/runs/{run_id}/logs`
- `GET /api/events/stream`

Library:
- `GET /api/library`
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
