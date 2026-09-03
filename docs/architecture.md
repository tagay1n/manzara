# Architecture index

Detailed invariants live in the nearest `AGENTS.md` or its routed guidance file.

| Concern | Owner/location | Focused verification |
| --- | --- | --- |
| Application assembly and routes | `app/factory.py`, `app/app_setup.py`, focused `app/*_routes.py` | `tests/test_api_*.py`, `tests/test_wiring_contracts.py` |
| Database repositories | `app/repositories/`; `app/db.py` is the facade | `tests/test_db.py`, repository-specific tests |
| Task runtime | `app/tasks.py`, `app/task_runtime/`, run artifact modules | task-runtime API tests, `tests/test_run_*.py` |
| Editable conveyor | `app/conveyor.py`, `app/repositories/conveyor.py`, `static/conveyor.js` | `tests/test_conveyor.py`, frontend tasks-page tests |
| Document eligibility | `app/document_sync_filter.py` | `tests/test_document_sync_filter.py` |
| Shared Gemini runtime | `app/gemini_*.py`; read `docs/gemini-runtime.md` | `tests/test_gemini_*.py` |
| Library flow | `app/modules/library/`; read its routing `AGENTS.md` | `tests/test_library_*.py` |
| Static Library publishing export | `app/modules/library/site_export*.py`, `app/modules/library/runtime/run_site_export.py` | `tests/test_library_site_export.py` |
| Maintenance flow | `app/modules/maintenance/`; read its routing `AGENTS.md` | Maintenance/storage-specific tests |
| PostgreSQL backup/recovery | `app/modules/maintenance/backup_s3_verify.py`, `scripts/migrate_pgbackrest_to_backblaze.py` | `tests/test_backup_s3_verify.py`, `tests/test_pgbackrest_migration.py` |
| PostgreSQL cloud migration | `scripts/migrate_postgres_to_aiven.py` | `tests/test_aiven_migration.py` |
| Frontend | `static/`; read `static/AGENTS.md` | `tests/frontend/` |
| Migrations | `alembic/versions/`; PostgreSQL is authoritative | migration tests, `alembic heads` |

Dependencies point inward: flow modules may use shared core; shared core must not import flow internals. The backend owns domain decisions, and frontend code renders backend contracts.
