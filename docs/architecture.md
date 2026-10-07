# Implementation map

Read the matching owner and nearest `AGENTS.md`, then search symbols. Do not preload all guidance.

The frontend is removed; API/SSE and task scripts remain. A rich operations CLI is pending. Catalog-dependent backend behavior needs adaptation and verification; [catalog model](catalog-model.md) records the current boundary.

| Concern | Start here |
| --- | --- |
| Server assembly, startup/shutdown | `app/main.py`, `app/factory.py`, `app/app_setup.py`, `app/bootstrap.py`, `app/dependencies.py` |
| API reads / controls / streams | `app/core_read_routes.py`, `app/control_routes.py`, `app/stream_routes.py`; domain routes: `app/library_*_routes.py` |
| Durable DB facade and repositories | `app/db.py`, `app/repositories/`, `app/postgres_engine.py` |
| Catalog contract, standalone admin | `app/catalog/`, `app/catalog_admin.py`, `app/catalog_routes.py`; [catalog model](catalog-model.md) |
| Local runtime schema | `app/local_state.py` |
| Task execution, logs, artifacts | `app/tasks.py`, `app/task_runtime/`, `app/run_log_store.py`, `app/run_artifact_channel.py`, `app/run_summary.py`; [task rules](../app/task_runtime/AGENTS.md) |
| Conveyor and workflow states | `app/conveyor.py`, `app/repositories/conveyor.py`, `app/runtime_states.py` |
| Gemini config, pool, quota, transport, pacing | `app/gemini_*.py`, `app/repositories/gemini*.py`; [Gemini contract](gemini-runtime.md) |
| Library | [Library rules](../app/modules/library/AGENTS.md), [owner lookup](../app/modules/library/guidance/navigation.md) |
| Maintenance | [Maintenance rules](../app/modules/maintenance/AGENTS.md) |
| Source storage and eligibility | `app/document_storage.py`, `app/document_sync_filter.py`, `app/document_cleanup_paths.py` |
| Schema migrations and import tools | `alembic/versions/`, `alembic/sql/`, `scripts/catalog_*.py` |
| Backups / exports | `.github/workflows/`, `scripts/backup_postgres_to_b2.py`; [operations](operations.md), [recovery](postgres-backup-recovery.md) |

## Database ownership

PostgreSQL owns durable domain data and workflow checkpoints. SQLite owns disposable orchestration and provider/retry state; see root rules for exact boundaries.

`app/postgres_engine.py` owns the bounded SQLAlchemy pools. Repositories share its engine per database/schema within a process; do not create independent engines. Child task processes normally use pool size 1; publisher analysis reserves 2 for its advisory-lock session and short transactions. Server default is 4. Local task/SSE/Gemini reads do not use PostgreSQL.

Alembic owns durable schema changes. `app/local_state.py` owns the disposable SQLite schema. Never infer the deployed schema from Alembic files alone.

Validation policy and coverage limits: [verification](verification.md). Update this map when ownership moves.
