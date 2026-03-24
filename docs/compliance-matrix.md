# Compliance Matrix

Last audit: 2026-03-24  
Scope: enforceable requirements from `AGENTS.md` (engineering constraints, frontend/backend standards, low-context scalability rules).

## Legend
- `PASS`: implemented and evidenced in code/tests/docs.
- `PARTIAL`: partly implemented; gaps remain.
- `FAIL`: not implemented (or contradicted by current code).
- `N/A`: policy/process requirement, not directly verifiable in code.

## Automated Baseline (current)
- `pytest`: `40 passed` (`.venv/bin/python -m pytest -q`)
- `frontend tests`: `node --test tests/frontend/*.mjs` passed (`core helpers` + `dashboard/schedules/tasks/task/library/database/classification-list/classification-detail/personality/publisher/normalization page behavior`)
- `requirements files`: only `requirements.txt` found (`rg --files -g 'requirements*.txt'`)

## Matrix
| ID | Requirement | Status | Evidence | Notes / Gap |
|---|---|---|---|---|
| EC-01 | PostgreSQL-only runtime store | PASS | `app/db.py:1`, `app/settings.py:40-89`, `README.md:113-116` | Runtime DB layer is PostgreSQL/psycopg2 based. |
| EC-02 | Default schema is `monocorpus` (`MANZARA_DB_SCHEMA`) | PASS | `app/settings.py:77`, `README.md:114-116` | Default applied at settings load. |
| EC-03 | Do not reintroduce SQLite runtime paths | PASS | `app/db.py` (PostgreSQL implementation), repo search has no SQLite runtime URL usage | A compatibility comment exists, but runtime path is PostgreSQL. |
| EC-04 | Keep secrets out of git (`config.yaml` local-only, masked `config.example.yaml`) | PASS | `.gitignore:9-11`, `README.md:123-131`, `config.example.yaml` redactions | `config.yaml` is not tracked by git. |
| EC-05 | Single dependency file policy | PASS | `requirements.txt` only, `README.md:99-101` | No split requirement files present. |
| EC-06 | Runtime-heavy flow coverage + manual smoke expectations documented | PASS | `README.md:214-218` | Manual checks documented for `meta_evaluate` and normalization refresh. |
| BE-01 | Every task run has artifact log under `_artifacts/task_runs/<task_id>/run-<run_id>.log` | PASS | `app/tasks.py:686-704`, `tests/test_api.py:831-849` | Covered by automated test. |
| BE-02 | Uniform artifact log line format | PASS | `app/tasks.py:740-756`, `tests/test_api.py:855-862`, `README.md:183-187` | Format is stable and documented. |
| BE-03 | Persist stdout to DB logs and mirror to artifact logs | PASS | `app/tasks.py:665-681`, `app/tasks.py:530-545` | DB + SSE + artifact mirror present. |
| BE-04 | Include explicit start/final status runtime lines | PASS | `app/tasks.py:279-286`, `app/tasks.py:364-374`, `tests/test_api.py:852-853` | Start and final outcome logged. |
| BE-05 | Keep secrets out of runtime logs | PASS | Redaction layer in `app/tasks.py` (`_sanitize_log_line`) applied to DB logs, SSE payload lines, and artifact logs; regression in `tests/test_api.py` (`test_task_logs_are_redacted_in_db_and_artifact_files`) | Masks common secret/token/password/key patterns and credential-style URLs. |
| BE-06 | Surface actionable errors in run state/logs/events | PARTIAL | `app/tasks.py:389-413`, `app/tasks.py:521-537` | Good coverage in task runner; still has broad exception swallowing in stream path (`app/tasks.py:682-684`). |
| FE-01 | Bootstrap via API, then apply important updates from SSE | PASS | `static/app.js:17-27`, `static/app.js:633-685` and same pattern in page scripts | Implemented across pages. |
| FE-02 | Backend API/SSE is source of truth; avoid frontend domain duplication | PARTIAL | API/SSE-driven flow in page scripts | Most state is server-driven; frontend still duplicates control helpers and transforms per page. |
| FE-03 | Route HTTP calls through shared client layer | PASS | Shared client in `static/core.js:40-50`; page scripts call it (`static/app.js:17-18`, `static/tasks.js:10-11`, etc.) | Transport/error handling now centralized in one module. |
| FE-04 | Explicit view-state model (`loading`, `ready`, `empty`, `error`) | PARTIAL | `static/tasks.js`, `static/task.js`, `static/app.js`, `static/schedules.js`, `static/library.js`, `static/database.js`, `static/library-classifications.js`, `static/library-classification.js`, `static/library-personalities.js`, `static/library-publishers.js`, `static/library-normalization.js` | Explicit states are now implemented across all operational pages; state reducers and helper abstractions are still page-local. |
| FE-05 | Add frontend behavior-focused tests where applicable | PARTIAL | `tests/frontend/test_core.mjs`, `tests/frontend/test_pages.mjs` (page behavior + normalization interaction tests for pagination, stop-all guard, suggestions refresh payload, bulk queue actions, suggestion accept/reject, merge, and history undo) | Coverage now includes key interaction paths on normalization page; end-to-end chained user flows across tabs still need more depth. |
| FE-06 | European datetime format (24h, day-first) | PASS | Shared formatter in `static/core.js:14-23`; page scripts use `window.ManzaraCore.formatDateTime(...)` | Enforced via `Intl.DateTimeFormat("en-GB", ...)`. |
| FE-07 | Keep timezone explicit when operational timestamps can be ambiguous | PASS | Shared datetime/time formatters include timezone by default (`static/core.js:13`, `static/core.js:30`) | Operational timestamps now render with timezone marker (for example `GMT+3`). |
| FE-08 | Use UI reference baseline (Mission Control) | PASS | `AGENTS.md:55-56`, `README.md:24-29` | Documented baseline for frontend direction. |
| FE-09 | Keep rendering safe; no unsanitized HTML injection | PARTIAL | Frequent `escapeHtml()` usage (`static/app.js:36-45`, etc.) | No centralized sanitizer contract/lint guard; needs automated checks. |
| LC-BE-01 | Prefer declarative registries/maps over branching for flow/task definitions | PASS | `app/modules/shayan/tasks.py`, `app/modules/maintenance/tasks.py`, workflow bundles in `app/modules/*/workflow.py` | Task/workflow seeds are declarative dict/list structures. |
| LC-BE-02 | Move overlap/catchup schedule behavior into policy data | PASS | schedule fields in workflow bundles (`overlap_policy`, `catchup_policy`) | Policy values stored in schedule config. |
| LC-BE-03 | Shared run/workflow state machine definitions | PARTIAL | `app/db.py` constants (`ACTIVE_STATUSES`, `ACTIVE_WORKFLOW_STATUSES`) | Transitions still spread across runtime/service methods. |
| LC-FE-01 | Centralize data/event handling utilities to reduce page-level branching | PARTIAL | Shared utilities in `static/core.js` (`api`, datetime/event banner, `applyStopAllButton`, `createSseController`, `escapeHtml`, `cssName`, status helpers) and page adoption across all page scripts | Core transport/time/SSE/sanitization/status logic is centralized; render/state orchestration is still duplicated across page modules. |

## Priority Remediation Batches
1. Frontend foundation extraction (phase 3):
   - Extract shared page primitives for tabbed data workbenches (status bars, table shells, error/empty blocks, pagination controls) used by normalization/personality/publisher/classification pages.
2. Frontend test baseline:
   - Expand interaction-level tests further (multi-step cross-tab flows, evidence dialog flows, queue+canonical creation sequences, richer filter transitions).
3. Logging hardening:
   - Expand redaction allowlist/denylist as new integrations are added and add fixture-based regression tests for new secret formats.
