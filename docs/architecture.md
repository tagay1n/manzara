# Implementation map

Read the matching owner and nearest `AGENTS.md`, then search symbols. Do not preload all guidance.

The inline CLI supports personality normalization, non-PDF extraction, and explicit cleanup review commands. Cleanup preparation and Yandex catalog sync run through standalone daily maintenance using the shared task runtime. Web pages and all HTTP APIs are retired. Other tasks remain disabled pending catalog adaptation and runtime verification; [catalog model](catalog-model.md) records the current boundary.

| Concern | Start here |
| --- | --- |
| CLI composition, keyboard interaction, lifecycle | `app/__main__.py`, `app/cli/` |
| Daily cleanup/sync composition and runner credentials | `scripts/run_daily_maintenance.py`, `scripts/prepare_maintenance_config.py`; [scheduled operations](operations.md) |
| Task descriptors, run contexts, session / batch ownership | `app/task_runtime/contracts.py`, `app/task_runtime/session.py`, `app/task_runtime/batch.py` |
| Durable DB facade and repositories | `app/db.py`, `app/repositories/`, `app/postgres_engine.py` |
| Catalog contract and transactional commands | `app/catalog/`; personality writes: `app/catalog/personality_normalization.py`; [catalog model](catalog-model.md) |
| Local runtime schema and flow operations | `app/local_state.py`, `app/operational_state.py`; personality composition: `app/repositories/personality_checkpoints.py` |
| Task execution, logs, artifacts | `app/tasks.py`, `app/task_runtime/`, `app/run_log_store.py`, `app/run_artifact_channel.py`; [task rules](../app/task_runtime/AGENTS.md) |
| Shared task run states and terminal event names | `app/runtime_states.py` |
| Gemini config, pool, quota, transport, pacing | `app/gemini_*.py`, `app/repositories/gemini*.py`; [Gemini contract](gemini-runtime.md) |
| Cleanup planning and CLI review | `app/modules/library/document_cleanup*.py`, `app/modules/library/cleanup_cli.py`, shared persistence: `app/repositories/document_cleanup.py`; [cleanup contract](document-cleanup.md) |
| Non-PDF source snapshots / durable writes | `app/catalog/non_pdf.py`; flow candidate/retry ownership: `app/modules/library/non_pdf_repository.py`; CLI handler: `app/modules/library/runtime/run_extract_non_pdf.py` |
| Library | [Library rules](../app/modules/library/AGENTS.md), [owner lookup](../app/modules/library/guidance/navigation.md) |
| Catalog sync commands | `app/catalog/document_sync.py`; set-based writes: `app/catalog/document_sync_bulk.py`; execution: `app/modules/maintenance/runtime/sync_monocorpus.py` |
| Maintenance | [Maintenance rules](../app/modules/maintenance/AGENTS.md) |
| Source storage and eligibility | `app/document_storage.py`, `app/document_sync_filter.py`, `app/document_cleanup_paths.py` |
| Durable schema baseline and future migrations | `alembic/versions/`, `alembic/sql/baseline_0062.sql`; [bootstrap and historical recovery policy](operations.md) |
| Backups / exports | `.github/workflows/`, `scripts/backup_postgres_to_b2.py`; Google Sheets: `app/modules/maintenance/dump_state.py`, shared snapshot: `app/catalog/export.py`; [operations](operations.md), [recovery](postgres-backup-recovery.md) |

## Database ownership

PostgreSQL owns durable domain data and workflow checkpoints. SQLite owns disposable orchestration and provider/retry state; see root rules for exact boundaries.

`app/postgres_engine.py` owns the bounded SQLAlchemy pools. Repositories share its engine per database/schema within a process; do not create independent engines. CLI tasks run in background threads and share the same pool, default size 4. Sync holds one pool connection for its schema-wide advisory lock and requires at least two connections so short mutation transactions can proceed. Retained standalone scripts have separate process-local budgets; publisher analysis reserves 2 for its advisory-lock session and short transactions. Local task/event/Gemini reads do not use PostgreSQL.

CLI and batch startup initialize only SQLite. Normalization, non-PDF extraction, and daily maintenance check the catalog read-only before work. Alembic owns durable schema changes and runs separately. `app/local_state.py` owns the disposable SQLite schema. Never infer the deployed schema from Alembic files alone.

Validation policy and coverage limits: [verification](verification.md). Update this map when ownership moves.
