# Implementation map

Read the relevant owner and nearest `AGENTS.md`, then search symbols; do not preload all guidance. Flow internals are owned by `app/modules/<flow>/`, shared contracts by `app/`. [README](../README.md) owns CLI tasks; [operations](operations.md) owns runners/configuration. Retained code must be reachable from CLI, workflow runners, or separate Alembic bootstrap.

| Concern | Owner / guidance |
| --- | --- |
| YAML settings/provisioning | `app/runtime_config.py`, `app/settings.py`, `scripts/prepare_workflow_config.py`, `.github/config/`; [configuration](operations.md#runtime-configuration) |
| CLI composition/interaction | `app/__main__.py`, `app/cli/`; `task_registry.py` composes flow-owned typed registrations for CLI and scheduled stages |
| CLI selection/command persistence | `app/cli/state.py` owns records in the existing local operational store; [local runtime state](operations.md#local-runtime-state) |
| Task contracts, session/batch lifecycle | `app/task_runtime/contracts.py`, `session.py`, `batch.py`; [task rules](../app/task_runtime/AGENTS.md) |
| Execution, logs, artifacts, states | `app/tasks.py`, `app/task_runtime/logging.py`, `artifacts.py`, `reporting.py`, `app/runtime_states.py` |
| PostgreSQL facade/repositories/pools | `app/db.py`, `app/repositories/`, `app/postgres_engine.py` |
| Catalog commands/schema | `app/catalog/`; [catalog contract](catalog-model.md) |
| Local orchestration/flow state | `app/local_state.py`, `app/operational_state.py`; personality store composition: `app/repositories/personality_checkpoints.py` |
| Gemini quota/transport/pacing | `app/gemini_*.py`, `app/repositories/gemini*.py`; [contract](gemini-runtime.md) |
| Library flows | [Library rules](../app/modules/library/AGENTS.md), [owner lookup](../app/modules/library/guidance/navigation.md) |
| Cleanup planning/review | `app/modules/library/document_cleanup*.py`, `cleanup_cli.py`, shared `app/repositories/document_cleanup.py`; [review contract](document-cleanup.md) |
| Maintenance flows | [Maintenance rules](../app/modules/maintenance/AGENTS.md); sync: `app/catalog/document_sync.py`, `document_sync_bulk.py`, `app/modules/maintenance/runtime/sync_monocorpus.py` |
| Document transfer/coordination | `app/catalog/document_transfer.py`, `app/document_operation_lock.py`, `app/modules/maintenance/runtime/sync_documents_s3.py`, `scripts/run_backblaze_transfer.py` |
| Source integrity/eligibility | `app/document_storage.py`, `app/s3_transfer.py` (caller-owned S3 clients and sequential transfer policy), `app/document_sync_filter.py`, `app/document_cleanup_paths.py` |
| Schema/bootstrap | `alembic/versions/`, `alembic/sql/baseline_0062.sql`; [database tools](operations.md#database-tools) |
| Backups/Sheets | `.github/workflows/`, `scripts/backup_postgres_to_b2.py`, `app/modules/maintenance/dump_state.py`, `app/catalog/export.py`; [recovery](postgres-backup-recovery.md) |
| Daily composition | `scripts/run_daily_maintenance.py`; [scheduled operations](operations.md#scheduled-sync-and-cleanup) |

## Database ownership

Repositories share `app/postgres_engine.py`'s bounded engine per database/schema within a process; never create independent engines. One task worker processes items sequentially; I/O/lease support threads share the YAML `database_pool_size` bound. Standalone processes have separate budgets; local task/Gemini reads use SQLite.

| Operation | Minimum pool | Reserved connection |
| --- | --- | --- |
| Yandex Sync | 3 | Schema advisory lock plus document remote-operation lock |
| Backblaze transfer / previews | 2 | Document operation lock |
| Publisher clustering | 2 | Session advisory lock |

Per-document locks serialize conflicting storage/catalog changes while unrelated documents may proceed. Startup initializes SQLite only; interactive tasks, daily maintenance, and transfer check the catalog read-only. Alembic changes durable schemas separately; `app/local_state.py` owns SQLite initialization. Inspect deployment rather than inferring it from migration files. [Verification](verification.md) records readiness limits. Update this map when ownership moves.
