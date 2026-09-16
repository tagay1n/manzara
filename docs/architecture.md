# Architecture index

Detailed invariants live in the nearest `AGENTS.md` or its routed guidance file.

Start with the matching row; use symbol search within its owners before reading entire files. For Library internals, use `app/modules/library/guidance/navigation.md` for responsibility-level owners and focused tests. Follow imports into shared core only when the behavior crosses that boundary.

| Concern | Owner/location | Focused verification |
| --- | --- | --- |
| Application assembly and routes | `app/factory.py`, `app/app_setup.py`, focused `app/*_routes.py` | `tests/test_api_*.py`, `tests/test_wiring_contracts.py` |
| Database repositories (durable PostgreSQL and local runtime) | `app/repositories/`, `app/local_state.py`; `app/db.py` is the facade | `tests/test_db.py`, `tests/test_local_state.py`, repository-specific tests |
| Task runtime | `app/tasks.py`, `app/task_runtime/`, run artifact modules | task-runtime API tests, `tests/test_run_*.py` |
| Proactive attention signals | `app/attention.py`, `app/attention_registry.py`, flow-owned providers | `tests/test_attention.py`, frontend shell/tasks tests |
| Editable conveyor | `app/conveyor.py`, `app/repositories/conveyor.py`, `static/conveyor.js` | `tests/test_conveyor.py`, frontend tasks-page tests |
| Document eligibility | `app/document_sync_filter.py` | `tests/test_document_sync_filter.py` |
| Shared Gemini runtime | `app/gemini_*.py`; read `docs/gemini-runtime.md` | `tests/test_gemini_*.py` |
| Library flow | `app/modules/library/AGENTS.md`; implementation/test lookup in `app/modules/library/guidance/navigation.md` | Select the focused tests from the Library lookup |
| Static Library publishing export | `app/modules/library/site_export*.py`, `app/modules/library/runtime/run_site_export.py` | `tests/test_library_site_export.py` |
| Maintenance flow | `app/modules/maintenance/`; read its routing `AGENTS.md` | Maintenance/storage-specific tests |
| PostgreSQL backup/recovery | `.github/workflows/nightly-postgres-backup.yml`, `scripts/backup_postgres_to_b2.py` | `tests/test_postgres_logical_backup.py` |
| PostgreSQL cloud migration | `scripts/migrate_postgres_to_aiven.py` | `tests/test_aiven_migration.py` |
| Frontend | `static/`; read `static/AGENTS.md` | `tests/frontend/` |
| Migrations | `alembic/versions/` for durable PostgreSQL; `app/local_state.py` for disposable SQLite | migration tests, `tests/test_local_state.py`, `alembic heads` |

Dependencies point inward: flow modules may use shared core; shared core must not import flow internals. The backend owns domain decisions, and frontend code renders backend contracts.

## Shared entry points

| Change | Start here | Focused verification |
| --- | --- | --- |
| App wiring, dependency injection | `app/factory.py`, `app/app_setup.py`; `app/dependencies.py` assembles per-application operations; `app/main.py` is the runtime entry point | `tests/test_wiring_contracts.py` |
| Run start/stop and control payloads | `app/control_routes.py`, `app/tasks.py`, `app/task_runtime/commands.py`, `app/task_runtime/process.py` | `tests/test_api_task_runtime.py` |
| SSE transport, snapshot cursors | `app/stream_routes.py`, `app/core_read_routes.py` | `tests/test_api_core.py`, `tests/test_api_task_runtime.py` |
| Run logs, artifacts, summaries | `app/run_log_store.py`, `app/run_artifact_channel.py`, `app/run_summary.py`; read `app/task_runtime/AGENTS.md` | `tests/test_run_log_store.py`, `tests/test_run_artifact_channel.py`, `tests/test_run_summary.py` |
| SQLite schema and runtime repositories | `app/local_state.py`, `app/repositories/runs.py`, `app/repositories/conveyor.py`, `app/repositories/gemini.py` | `tests/test_local_state.py`, repository concern's focused tests |
| Gemini config / model fallback / quota leases / transport | `app/gemini_config.py` / `app/gemini_model_pool.py` / `app/gemini_runtime.py` / `app/gemini_requests.py` | Corresponding `tests/test_gemini_*.py` file |
| Frontend HTTP and shared shell | `static/core.js`, `static/shell.js`; read `static/AGENTS.md` | `tests/frontend/test_core.mjs`, `tests/frontend/test_shell.mjs`, `tests/frontend/test_shell_state.mjs` |

Commands and environment requirements: `docs/verification.md`. Update navigation when moving an owner or changing its focused-test coverage; avoid recording line numbers or test counts.
