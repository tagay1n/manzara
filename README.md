# Manzara

Manzara runs Tatar-language content workflows through an inline terminal CLI and operational scripts. Web pages, HTTP APIs, and SSE transport are retired.

## Current status

**Normalize personalities** and **Cleanup plan** are enabled CLI tasks. It uses the normalized PostgreSQL catalog, the shared Gemini runtime, and resumable checkpoints. Other tasks remain visible but disabled pending catalog adaptation. No credential-backed execution has been performed to establish runtime readiness.

## Setup and launch

Use Python 3.10+ and PostgreSQL. Install dependencies in the existing environment:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m app
```

Copy the masked structure of `config.example.yaml` to a gitignored `config.local.yaml` or `config.yaml`. Runtime configuration resolves `MANZARA_CONFIG_PATH`, then local configuration; never load the example at runtime.

Optional `--workers N`, `--limit N`, and `--task TASK_ID` select next-run settings and the initial task. Launching never starts a task automatically. Execution requires an interactive terminal; `--help` works without configuration.

## Controls and lifecycle

- ↑/↓ selects tasks; Enter opens actions. Tab switches focus, and Esc returns to the task list.
- Actions offer Start/Resume, Stop safely, Settings, Logs, Summary, and Recent runs. Workers and the optional candidate limit are fixed for each active run.
- Live progress and elapsed time remain visible while navigating. Logs use bounded pages: PageUp/Down reads older/newer pages; `f` resumes following.
- `q` or Ctrl-C requests safe stop and waits for active requests to reach their checkpoints before exiting. Provider requests have a 60-second I/O timeout, not a hard total-duration limit. Threads are never force-killed automatically.
- Reopening preserves compatible completed decisions and resumes eligible work. One CLI session owns each local runtime store; a second session reports the conflict.

CLI startup initializes local SQLite, seeds definitions without pruning history, and recovers interrupted local runs. It does **not** apply PostgreSQL migrations. Normalization checks the catalog read-only before processing; incompatible schemas must be migrated separately.

| Setting | Purpose / default |
| --- | --- |
| `MANZARA_DATABASE_URL` | Durable PostgreSQL URL; may also come from local YAML |
| `MANZARA_DB_SCHEMA` | Domain schema; `monocorpus` |
| `MANZARA_ALEMBIC_VERSION_SCHEMA` | Migration version-table schema; defaults to the domain schema |
| `MANZARA_CONFIG_PATH` | Explicit configuration path |
| `MANZARA_DB_POOL_SIZE` | Shared CLI PostgreSQL pool bound; default 4 |
| `MANZARA_ARTIFACTS_ROOT` | Artifact root; `~/.manzara` |
| `MANZARA_LOCAL_STATE_PATH` | Disposable SQLite runtime; `~/.manzara/state/runtime.sqlite3` |

Every enabled task shares the process's bounded PostgreSQL engine. Definitions, runs, events, Gemini coordination, and AI retry exclusions remain in local SQLite; domain data and safety-critical checkpoints remain in PostgreSQL.

Logs live at `~/.manzara/logs/task-runs/<task_id>/run-<run_id>.log`, with structured summaries alongside them and persisted `task.artifact` events. Overrides use the configured artifacts root.

Gemini models and account/project-grouped keys come from local configuration, with no model default. See the [Gemini contract](docs/gemini-runtime.md).

## Guidance

- [Repository rules](AGENTS.md) and [architecture](docs/architecture.md).
- [Personality normalization](docs/personality-normalization.md), [cleanup planning and review](docs/document-cleanup.md), and [catalog model](docs/catalog-model.md).
- [Verification limits](docs/verification.md) and [active work](TODO.md).
- [Operations](docs/operations.md) and [PostgreSQL recovery](docs/postgres-backup-recovery.md).
