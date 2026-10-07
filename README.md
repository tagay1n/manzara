# Manzara

Manzara runs Tatar-language content workflows through a FastAPI backend, Library and Maintenance workers, and operational scripts.

## Current status

- The web frontend has been removed. The API and SSE transport remain; there are no browser pages.
- A rich operations CLI has not been implemented. Existing task scripts are individual entry points.
- The owner reports that the PostgreSQL catalog was migrated but the backend is not fully adapted and is broken. Existing adapters do not establish end-to-end readiness. See [catalog model](docs/catalog-model.md) and [active work](TODO.md).

## Setup and configuration

Use Python 3.10+ and PostgreSQL. Enabled workflows also require their configured services and converter binaries; inspect the matching module guidance before running them.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Copy the masked structure of `config.example.yaml` to a gitignored `config.local.yaml` or `config.yaml`. Runtime configuration resolves `MANZARA_CONFIG_PATH`, then `config.local.yaml`, then `config.yaml`; never load the example at runtime.

| Setting | Purpose / default |
| --- | --- |
| `MANZARA_DATABASE_URL` | Durable PostgreSQL URL; may also come from local YAML |
| `MANZARA_DB_SCHEMA` | Domain schema; `monocorpus` |
| `MANZARA_CONFIG_PATH` | Explicit configuration path |
| `MANZARA_DB_POOL_SIZE` | Per-process PostgreSQL pool bound; server default 4 |
| `MANZARA_ARTIFACTS_ROOT` | Artifact root; `~/.manzara` |
| `MANZARA_LOCAL_STATE_PATH` | Disposable SQLite runtime; `~/.manzara/state/runtime.sqlite3` |

Gemini models and account/project-grouped keys come from local configuration, with no model default. See [Gemini contract](docs/gemini-runtime.md).

## Backend entry point

The following is the retained server entry point, not confirmation that catalog-dependent operations work:

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload --timeout-graceful-shutdown 10
```

Startup initializes local SQLite, applies PostgreSQL Alembic migrations, seeds task definitions, and recovers runtime state. Starting the server can therefore change the database schema; use offline checks while investigating the current mismatch.

API routes live in `app/*_routes.py`; inspect `/docs` on a configured running backend for its generated API reference. Task logs live at `~/.manzara/logs/task-runs/<task_id>/run-<run_id>.log`.

## Read only what the task needs

- [AGENTS.md](AGENTS.md): repository rules.
- [Architecture](docs/architecture.md): implementation owners.
- [Catalog model](docs/catalog-model.md): PostgreSQL contract and adaptation gap.
- [Verification](docs/verification.md): checks available in this checkout.
- [Operations](docs/operations.md): storage and scheduled exports.
- [Backup and recovery](docs/postgres-backup-recovery.md): backup configuration and restore procedure.

Module guidance is routed by the nearest `AGENTS.md`. Keep docs focused on current contracts, unresolved work, and repeatable operations; completed-task narratives belong in git history.
