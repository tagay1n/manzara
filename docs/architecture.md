# Implementation map

Read the matching owner and nearest `AGENTS.md`, then search symbols. Do not preload all guidance.

The inline CLI supports personality normalization, non-PDF extraction, book preview generation, static Library export, publisher clustering proposals, collection discovery proposals, and explicit cleanup review commands. Cleanup preparation and Yandex catalog sync run through standalone daily maintenance using the shared task runtime. Web pages and all HTTP APIs are retired. Other tasks remain disabled pending catalog adaptation and runtime verification; [catalog model](catalog-model.md) records the current boundary.

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
| Publisher clustering inventory / checkpoints / proposals | `app/catalog/publisher_analysis.py`; execution: `app/modules/library/runtime/run_suggest_publisher_merges.py`; [publisher contract](../app/modules/library/guidance/publisher-merges.md) |
| Collection discovery inventory / proposals | `app/catalog/collection_discovery.py`; rules: `app/modules/library/collection_detection.py`; execution: `app/modules/library/runtime/run_collection_detect.py`; [collection contract](../app/modules/library/guidance/collections.md) |
| Book preview selection / claims / rendering | `app/catalog/book_previews.py`, `app/catalog/previews.py`; execution: `app/modules/library/runtime/run_generate_book_previews.py`, `app/modules/library/catalog_preview_worker.py`; [document rules](../app/modules/library/guidance/documents.md#sources-and-previews) |
| Static Library snapshot / bundle / publication | `app/modules/library/site_export_repository.py`, `app/modules/library/site_export.py`, `app/modules/library/runtime/run_site_export.py`; [export contract](../app/modules/library/guidance/site-export.md) |
| Library | [Library rules](../app/modules/library/AGENTS.md), [owner lookup](../app/modules/library/guidance/navigation.md) |
| Catalog sync commands | `app/catalog/document_sync.py`; set-based writes: `app/catalog/document_sync_bulk.py`; execution: `app/modules/maintenance/runtime/sync_monocorpus.py` |
| Maintenance | [Maintenance rules](../app/modules/maintenance/AGENTS.md) |
| Backblaze transfer queue/checkpoints and document coordination | `app/catalog/document_transfer.py`, `app/document_operation_lock.py`; execution: `app/modules/maintenance/runtime/sync_documents_s3.py`; workflow runner: `scripts/run_backblaze_transfer.py` |
| Source storage and eligibility | `app/document_storage.py`, `app/document_sync_filter.py`, `app/document_cleanup_paths.py` |
| Durable schema baseline and future migrations | `alembic/versions/`, `alembic/sql/baseline_0062.sql`; [bootstrap and historical recovery policy](operations.md) |
| Backups / exports | `.github/workflows/`, `scripts/backup_postgres_to_b2.py`; Google Sheets: `app/modules/maintenance/dump_state.py`, shared snapshot: `app/catalog/export.py`; [operations](operations.md), [recovery](postgres-backup-recovery.md) |

## Database ownership

PostgreSQL owns durable domain data and workflow checkpoints. SQLite owns disposable orchestration and provider/retry state; see root rules for exact boundaries.

`app/postgres_engine.py` owns the bounded SQLAlchemy pools. Repositories share its engine per database/schema within a process; do not create independent engines. CLI tasks run in background threads and share the same pool, default size 4. Sync holds one pool connection for its schema-wide advisory lock and another during a document remote operation; it requires at least three connections so short mutation transactions can proceed. Transfer reserves one connection for its document session lock and requires at least two. Both workflows configure size 4. Per-document operation locks serialize conflicting storage and catalog changes while unrelated documents can proceed. Book previews reserve one pool connection for a document operation lock and require at least two connections. Publisher clustering shares the CLI pool and requires at least two connections for its advisory-lock session and short transactions. Retained standalone scripts have separate process-local budgets. Local task/event/Gemini reads do not use PostgreSQL.

CLI and batch startup initialize only SQLite. Normalization, non-PDF extraction, book previews, static export, publisher clustering, collection discovery, daily maintenance, and Backblaze transfer check the catalog read-only before work. Alembic owns durable schema changes and runs separately. `app/local_state.py` owns the disposable SQLite schema. Never infer the deployed schema from Alembic files alone.

Validation policy and coverage limits: [verification](verification.md). Update this map when ownership moves.
