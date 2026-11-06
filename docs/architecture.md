# Implementation map

Read the matching row, nearest `AGENTS.md`, and only the contract sections relevant to the change; then search symbols. Do not chain through flow navigation indexes. Flow internals are owned by `app/modules/<flow>/`, shared contracts by `app/`. [README](../README.md) owns CLI tasks; [operations](operations.md) owns runners/configuration. Retained code must be reachable from CLI, workflow runners, or separate Alembic bootstrap.

| Concern | Owner / guidance |
| --- | --- |
| YAML settings/provisioning | `app/runtime_config.py`, `app/settings.py`, `scripts/prepare_workflow_config.py`, `.github/config/`; [configuration](operations.md#runtime-configuration) |
| CLI composition/interaction | `app/__main__.py`, `app/cli/`; `task_registry.py` composes flow-owned typed registrations for CLI and scheduled stages |
| CLI picker/settings presentation | `app/cli/picker.py`, `app/cli/settings_form.py`; `app/cli/terminal.py` owns interaction/settings, `app/cli/lifecycle.py` owns foreground start/stop/finalization through output drain |
| CLI selection/command persistence | `app/cli/state.py` owns records in the existing local operational store; [local runtime state](operations.md#local-runtime-state) |
| CLI saved task defaults | `app/cli/settings.py` owns editable fields and input parsing; `app/task_settings.py` owns task-specific paths, fields and validation; `app/runtime_config.py` owns private-YAML snapshots and atomic writes; [configuration](operations.md#runtime-configuration) |
| Task contracts, session/batch lifecycle | `app/task_runtime/contracts.py`, `session.py`, `batch.py`; [task rules](../app/task_runtime/AGENTS.md) |
| Execution, logs, artifacts, states | `app/tasks.py`, `app/task_runtime/logging.py`, `artifacts.py`, `reporting.py`, `app/runtime_states.py` |
| PostgreSQL facade/repositories/pools | `app/db.py`, `app/repositories/`, `app/postgres_engine.py` |
| Catalog commands/schema | `app/catalog/`; [catalog contract](catalog-model.md) |
| Metadata extraction/review queue, admin validation, and evaluation atomic saves | `app/catalog/metadata_processing.py`, `app/catalog/metadata_save.py`, `app/catalog/metadata_save.sql`, `app/catalog/repository.py`; [discovery](../app/modules/library/guidance/metadata.md#discovery-and-ownership), [persistence](../app/modules/library/guidance/metadata.md#persistence-and-protections), [retries](../app/modules/library/guidance/metadata.md#retry-state-and-output) |
| Shared Library metadata contracts | `app/modules/library/metadata_schema.py`, `metadata_normalization.py`, `metadata_urls.py`; catalog validation: `app/catalog/schema_org.py`; envelope types: `app/catalog/metadata_records.py`; classification projection: `app/catalog/metadata_projection.py` |
| Metadata extraction/reassessment | `app/modules/library/runtime/run_metadata_processing.py`, `app/modules/library/metadata_extraction.py`, `metadata_prompt.py`, `runtime/metadata/processing_request.py`; [extraction evidence](../app/modules/library/guidance/metadata.md#extraction-evidence-and-validation) |
| Metadata evaluation | Shared runner above; `app/modules/library/runtime/metadata/evaluation_evidence.py`, `evaluation_response.py`, `evaluation_patch.py`, `runtime/prompts/metadata_evaluation.py`; [evaluation evidence](../app/modules/library/guidance/metadata.md#evaluation-evidence-and-validation) |
| Library converters | `app/modules/library/non_pdf_*.py`, `google_doc_conversion.py`, `google_presentation_conversion.py`, `google_drive_conversion.py`; [documents](../app/modules/library/guidance/documents.md) |
| Collection proposals / static export | `app/modules/library/collection_detection.py`, `site_export.py` (public records), `site_export_bundle.py` (archive/verification), matching runtime runners; [collections](../app/modules/library/guidance/collections.md), [export](../app/modules/library/guidance/site-export.md) |
| Publisher proposals | `app/modules/library/publisher_codex.py`, `publisher_merge_contract.py`, `runtime/run_suggest_publisher_merges.py`; [publishers](../app/modules/library/guidance/publisher-merges.md) |
| Personality discovery/atomic decisions | `app/catalog/personality_candidates.py` / `.sql`, `app/catalog/personality_normalization.py` / `.sql`; [personality contract](personality-normalization.md) |
| One-message pooled catalog commands | `app/catalog/command_batch.py`; metadata and personality commands share connection cleanup and uncertain-commit handling |
| Preview discovery/atomic claims and publication | `app/catalog/book_previews.py`, `app/catalog/book_preview_preflight.sql`, `app/catalog/previews.py`, `app/catalog/previews.sql`; [preview contract](../app/modules/library/guidance/documents.md#sources-and-previews) |
| Local orchestration/flow state | `app/local_state.py`, `app/operational_state.py`; personality store composition: `app/repositories/personality_checkpoints.py` |
| Gemini quota/transport/pacing | `app/gemini_*.py`, `app/repositories/gemini*.py`; [contract](gemini-runtime.md) |
| Library flows | [Library rules](../app/modules/library/AGENTS.md); task composition: `app/modules/library/tasks.py`; focused owners below |
| Cleanup planning/review | `app/modules/library/document_cleanup*.py`, `cleanup_cli.py`, shared `app/repositories/document_cleanup.py`; [review contract](document-cleanup.md) |
| Maintenance flows | [Maintenance rules](../app/modules/maintenance/AGENTS.md); sync: `app/catalog/document_sync.py`, `document_sync_bulk.py`, `app/modules/maintenance/runtime/sync_monocorpus.py` |
| Document row locking | `app/catalog/document_locks.py` owns the single-document operation/advisory/row lock sequence; callers own transactions and batch locking |
| Restricted sharing URLs | `app/document_sharing.py` owns encryption/decryption for core storage and Maintenance |
| Document transfer/coordination | `app/catalog/document_transfer.py`, `app/document_operation_lock.py`, `app/modules/maintenance/runtime/sync_documents_s3.py`, `scripts/run_backblaze_transfer.py` |
| Source integrity/eligibility | `app/document_storage.py`, `app/s3_transfer.py` (caller-owned S3 clients and sequential transfer policy), `app/s3_cleanup.py` (permanent version deletion; caller-owned domain guards/key selection), `app/document_sync_filter.py`, `app/document_cleanup_paths.py` |
| Schema/bootstrap and CLI startup upgrades | `app/migrations.py`, `alembic/env.py`, `alembic/versions/`, `alembic/sql/baseline_0062.sql`; [database tools](operations.md#database-tools) |
| Backups/Sheets | `.github/workflows/`, `scripts/backup_postgres_to_b2.py`, `app/modules/maintenance/dump_state.py`, `app/catalog/export.py`; [recovery](postgres-backup-recovery.md) |
| Daily composition | `scripts/run_daily_maintenance.py`; [scheduled operations](operations.md#scheduled-sync-and-cleanup) |

## Database ownership

Repositories share `app/postgres_engine.py`'s bounded engine per database/schema within a process; never create independent engines. One task worker processes items sequentially; I/O/lease support threads share the YAML `database_pool_size` bound. Standalone processes have separate budgets; local task/Gemini reads use SQLite.

| Operation | Minimum pool | Reserved connection |
| --- | --- | --- |
| Yandex Sync | 3 | Schema advisory lock plus document remote-operation lock |
| Backblaze transfer / previews | 2 | Document operation lock |
| Publisher clustering | 2 | Session advisory lock |

Per-document locks serialize conflicting storage/catalog changes while unrelated documents may proceed. Interactive CLI startup uses Alembic to apply pending PostgreSQL migrations before enabling tasks and initializing SQLite. Task preflights, daily maintenance, and transfer inspect the catalog read-only; scheduled runners do not migrate. `app/migrations.py` owns interactive upgrades; `app/local_state.py` owns SQLite initialization. Inspect deployment rather than inferring it from migration files. [Verification](verification.md) records readiness limits. Update this map when ownership moves.
