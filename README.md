# manzara

Manzara is a cloud-console operations dashboard for Tatar-language content workflows.

The name means a panorama or landscape opening before the viewer (`Манзара`). The UI is independently implemented for this repository and takes visual direction from [builderz-labs/mission-control](https://github.com/builderz-labs/mission-control).

## Architecture

- FastAPI backend and PostgreSQL runtime state
- Alembic-managed schema; no runtime DDL or SQLite runtime path
- Modular Library and Maintenance flows in one monorepo
- Server-sent events for live task state, progress, artifacts, and logs
- S3-compatible primary document storage with Yandex Disk as legacy upstream storage

See `docs/architecture.md` for the ownership map. Operational invariants live in the nearest `AGENTS.md` and its routed module guidance.

## Pages

- `/tasks` and `/tasks/{task-slug-or-id}`
- `/database`
- `/gemini`
- `/library`
- `/library/classifications` and `/library/classifications/{classification_id}`
- `/library/personalities`, `/library/publishers`, and `/library/collections`
- `/library/document-cleanup`
- `/library/normalization/{personality|publisher}`

`/` and `/dashboard` redirect to `/tasks`.

## Requirements and setup

- Python 3.10+
- PostgreSQL
- The local Monocorpus repository when running embedded Library/Maintenance workflows
- External binaries required by enabled document converters

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Manzara intentionally keeps one Python dependency file: `requirements.txt`.

## Configuration

Copy the masked structure from `config.example.yaml` into a local, gitignored `config.yaml` or `config.local.yaml`. Embedded runtimes resolve configuration in this order:

1. `MANZARA_CONFIG_PATH`
2. `./config.local.yaml`
3. `./config.yaml`

Common environment variables:

- `MANZARA_DATABASE_URL` — PostgreSQL URL
- `MANZARA_DB_SCHEMA` — defaults to `monocorpus`
- `MANZARA_CONFIG_PATH` — explicit YAML path
- `MANZARA_ARTIFACTS_ROOT` — defaults to `~/.manzara`
- `MONOCORPUS_REPO_PATH` — defaults to `/home/tans1q/projects/monocorpus`
- `PG_BACKREST_STANZA` — pgBackRest stanza name, default `monocorpus`
- `MANZARA_PGBACKREST_S3_BUCKET`, `MANZARA_PGBACKREST_S3_ENDPOINT`, and
  `MANZARA_PGBACKREST_S3_REGION` — optional overrides for `backups.pgbackrest`

PostgreSQL physical backups use the dedicated `backups.pgbackrest` S3 contract.
See `docs/postgres-backup-recovery.md` for migration, validation, and recovery.

Gemini configuration contains one ordered `gemini.model_pool` and account-grouped keys. Models have no code default. See `docs/gemini-runtime.md` for runtime behavior.

## Run

```bash
.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8080 \
  --reload \
  --timeout-graceful-shutdown 10
```

Startup applies pending Alembic migrations before seeding panel and task definitions.

Task artifact logs are written to:

```text
~/.manzara/task_runs/<task_id>/run-<run_id>.log
```

The browser uses PostgreSQL-backed API/SSE state; artifact files are for durable inspection.

## Database migrations

```bash
PYTHONPATH=. .venv/bin/alembic current
PYTHONPATH=. .venv/bin/alembic heads
PYTHONPATH=. .venv/bin/alembic upgrade head
```

The direct SQLite-to-PostgreSQL migration reference is retained in `docs/postgres-cutover.md` for historical operations only.

## Tests

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
node --test tests/frontend/*.mjs
PYTHONPATH=. .venv/bin/python -m ruff check app tests
```

Use focused files while iterating. Credential-backed Gemini, Backblaze, Yandex, converter, and pgBackRest workflows still require deliberate smoke testing against configured services. The stable verification checklist is in `docs/verification.md`.

## API entry points

- `GET /api/health`
- `GET /api/system/state`
- `GET /api/tasks`
- `GET /api/tasks/{task_id_or_slug}`
- `POST /api/tasks/{task_id}/toggle`
- `GET /api/runs/{run_id}/logs`
- `GET /api/events/stream`
- `GET /api/database/state`
- `GET /api/gemini/state`
- `GET /api/library`

Feature-specific endpoints are defined in the focused `app/*_routes.py` modules.
