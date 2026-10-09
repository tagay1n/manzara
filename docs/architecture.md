# Implementation map

Read the matching owner and nearest `AGENTS.md`, then search symbols. Do not preload all guidance.

The inline CLI supports personality normalization, cleanup preparation, and Yandex catalog sync, with explicit cleanup review commands. Web pages and all HTTP APIs are retired. Other tasks remain disabled pending catalog adaptation and runtime verification; [catalog model](catalog-model.md) records the current boundary.

| Concern | Start here |
| --- | --- |
| CLI composition, keyboard interaction, lifecycle | `app/__main__.py`, `app/cli/` |
| Task descriptors, run contexts, session ownership | `app/task_runtime/contracts.py`, `app/task_runtime/session.py` |
| Durable DB facade and repositories | `app/db.py`, `app/repositories/`, `app/postgres_engine.py` |
| Catalog contract and transactional commands | `app/catalog/`; personality writes: `app/catalog/personality_normalization.py`; [catalog model](catalog-model.md) |
| Local runtime schema and flow operations | `app/local_state.py`, `app/operational_state.py`; personality composition: `app/repositories/personality_checkpoints.py` |
| Task execution, logs, artifacts | `app/tasks.py`, `app/task_runtime/`, `app/run_log_store.py`, `app/run_artifact_channel.py`, `app/run_summary.py`; [task rules](../app/task_runtime/AGENTS.md) |
| Retained conveyor state and shared workflow states | `app/repositories/conveyor.py`, `app/runtime_states.py`; conveyor execution is unavailable |
| Gemini config, pool, quota, transport, pacing | `app/gemini_*.py`, `app/repositories/gemini*.py`; [Gemini contract](gemini-runtime.md) |
| Cleanup planning and CLI review | `app/modules/library/document_cleanup*.py`, `app/modules/library/cleanup_cli.py`, shared persistence: `app/repositories/document_cleanup.py`; [cleanup contract](document-cleanup.md) |
| Library | [Library rules](../app/modules/library/AGENTS.md), [owner lookup](../app/modules/library/guidance/navigation.md) |
| Catalog sync commands | `app/catalog/document_sync.py`; set-based writes: `app/catalog/document_sync_bulk.py`; inline execution: `app/modules/maintenance/runtime/sync_monocorpus.py` |
| Maintenance | [Maintenance rules](../app/modules/maintenance/AGENTS.md) |
| Source storage and eligibility | `app/document_storage.py`, `app/document_sync_filter.py`, `app/document_cleanup_paths.py` |
| Durable schema baseline and future migrations | `alembic/versions/`, `alembic/sql/baseline_0062.sql`; [bootstrap and historical recovery policy](operations.md) |
| Backups / exports | `.github/workflows/`, `scripts/backup_postgres_to_b2.py`; [operations](operations.md), [recovery](postgres-backup-recovery.md) |

## Database ownership

PostgreSQL owns durable domain data and workflow checkpoints. SQLite owns disposable orchestration and provider/retry state; see root rules for exact boundaries.

`app/postgres_engine.py` owns the bounded SQLAlchemy pools. Repositories share its engine per database/schema within a process; do not create independent engines. CLI tasks run in background threads and share the same pool, default size 4. Sync holds one pool connection for its schema-wide advisory lock and requires at least two connections so short mutation transactions can proceed. Retained standalone scripts have separate process-local budgets; publisher analysis reserves 2 for its advisory-lock session and short transactions. Local task/event/Gemini reads do not use PostgreSQL.

CLI startup initializes only SQLite; normalization and Sync check the catalog read-only before work. Alembic owns durable schema changes and runs separately. `app/local_state.py` owns the disposable SQLite schema. Never infer the deployed schema from Alembic files alone.

Validation policy and coverage limits: [verification](verification.md). Update this map when ownership moves.
