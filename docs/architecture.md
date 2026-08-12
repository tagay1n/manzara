# Architecture index

This is a short ownership map. Detailed operational invariants live in the nearest `AGENTS.md`.

| Concern | Owner/location |
| --- | --- |
| Application assembly and routes | `app/factory.py`, `app/app_setup.py`, and focused `app/*_routes.py` modules |
| Database repositories | `app/repositories/`; `app/db.py` is the stable compatibility facade |
| Task runtime | `app/tasks.py` orchestrates; process, command, and logging concerns live in `app/task_runtime/` |
| Workflow runtime | `app/workflows.py` and shared state definitions in `app/runtime_states.py` |
| Editable task conveyor | `app/conveyor.py` executes the singleton staged plan; persistence lives in `app/repositories/conveyor.py`; `static/conveyor.js` owns the editor above the task catalog |
| Shared Gemini runtime | `app/gemini_runtime.py`, `app/gemini_model_pool.py`, and `app/gemini_requests.py` |
| Flow modules | `app/modules/<flow>/`; each flow owns its tasks, runtime, config, repositories, and nested guidance |
| Frontend | shared transport/shell/UI in `static/core.js`, `static/shell.js`, and `static/ui.js`; page controllers and domain renderers are separate files |
| Migrations | `alembic/versions/`; PostgreSQL schema is the persisted source of truth |
| Tests | backend in `tests/`, browser behavior in `tests/frontend/`; use focused tests while iterating and the full suite before commit |

Dependencies point inward: flow modules may use shared core; shared core must not import flow internals. The backend owns domain decisions, and frontend code renders backend contracts.
