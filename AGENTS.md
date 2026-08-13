# AGENTS.md

Last updated: 2026-08-12
Owner: tans1q

## Scope

Manzara is a single monorepo for Tatar-language content operations and data workflows. Keep the monorepo modular: flow-specific code and guidance belong under `app/modules/<flow>/`, while shared runtime, database, API, and UI infrastructure belongs under `app/` and `static/`.

See `docs/architecture.md` for the ownership index. More specific instructions live in nested `AGENTS.md` files and apply to their directory trees.

## Global invariants

- PostgreSQL is the only runtime state store (`MANZARA_DATABASE_URL`), using schema `monocorpus` by default (`MANZARA_DB_SCHEMA`). Do not add SQLite runtime paths.
- The backend is the only source of business and data truth. Frontend state is limited to rendering, interaction, transport, and transient UI concerns.
- Write task and flow artifacts only under `~/.manzara`, or `MANZARA_ARTIFACTS_ROOT` when explicitly configured. Never create repository-root runtime artifact directories.
- Keep secrets out of git and logs. `config.yaml` is local-only; keep the masked `config.example.yaml` structurally current, but never load it at runtime.
- Keep one dependency file, `requirements.txt`, unless the owner explicitly requests another. Add dependencies when embedded runtime code gains external imports.
- Prefer clean forward code changes over compatibility branches. Persisted database compatibility is the exception: ask the owner before deciding on a migration or compatibility policy.
- Prefer declarative registries and shared contracts over central conditional routing. Keep functions small, nesting shallow, and side effects at module boundaries.
- Preserve explicit module ownership. Cross-flow imports are forbidden except through shared core modules.

## Task runtime and observability

- Every task supports graceful stop at a safe boundary and restart from persisted checkpoints.
- Every run has a dedicated log at `~/.manzara/task_runs/<task_id>/run-<run_id>.log` (or the configured artifacts root).
- Runtime lines use one structured format with timestamp, level, run/task/panel/source context, and message. Persist visible stdout/stderr in PostgreSQL and mirror it to the artifact log.
- Log meaningful start, per-item, decision, failure, and final-summary boundaries; include stable identifiers for successful mutations.
- Structured artifacts never come from parsing logs. Emit compact `task.artifact` SSE payloads, persist them in PostgreSQL, and expose large details through paginated endpoints.
- Log reads use bounded cursor pagination (`after_log_id` for follow and `before_log_id` for backfill).
- No silent failures: preserve actionable context in run state, logs, and events.
- The editable conveyor is one PostgreSQL-backed global definition. Steps execute visually from left to right; tasks stacked within one step execute in parallel. Running and completed steps are immutable, while future steps may be edited.

## Backend standards

- Backend changes are TDD-first by default. Add or adjust a failing focused test, implement, refactor, then run the relevant focused suite. Run the full suite before commit.
- Validate external control payloads strictly. Boolean-like fields use explicit allowlists; integer fields must be integral, never silently truncated.
- Prefer explicit API and SSE contracts and backward-compatible schema changes.
- Keep shared run/workflow states in one state-machine definition.

## Frontend standards

- Use the mission-control project as visual direction while retaining Manzara information architecture.
- Keep the shared shell responsible for navigation, command palette, connections, global task state, dialogs, toasts, logs, and footer behavior. Page scripts own page-specific rendering and intent.
- Route HTTP through the shared client, model `loading`, `ready`, `empty`, and `error` explicitly, and bootstrap each page from its own API snapshot.
- Snapshot payloads expose `event_cursor` captured before state composition. Seed that page's SSE stream from it; never replay from zero or borrow another snapshot cursor.
- Apply frequent task lifecycle/progress events directly. `task.log` never causes broad reloads or full rerenders; coalesce relevant terminal/artifact refreshes.
- Live counters come from explicit SSE artifact events. Detailed lists come from backend endpoints backed by PostgreSQL.
- Reuse shared components and tokens, render safely, serve deterministic local assets, and avoid browser `alert`, `confirm`, and `prompt` dialogs.
- Keep accessibility basics: semantic structure, keyboard navigation, visible focus, labels, and sufficient contrast.
- Task log viewers share tail/follow/backfill behavior and cursor pagination.
- Flow pages show flow stats and tasks. Task pages own task history, newest first, with a default page size of 20.
- Display operational time using a 24-hour clock and day-first dates; show timezone where ambiguous.
- Frontend behavior changes are TDD-first where meaningful. Cosmetic-only changes may use documented manual verification.

## Shared Gemini runtime

- All Gemini use goes through the shared runtime manager. Keys are grouped by account; runtime state is PostgreSQL-backed per `(account, key, model)`.
- Resolve model aliases and pools from config. Do not hardcode model names in task logic.
- Choose a random account, then a random usable key; try alternatives before waiting. Permit at most one request per minute per `(account, key, model)`.
- Infer daily limits from responses. Exhaustion is model-scoped and clears at reset rollover.
- A `429` fails the task by default; declared model-pool workflows may persist the attempt and advance through the pool. A `400` rejects only the request/item. A `5xx` starts a global 60-second pause.
- Transport failures share a bounded retry budget with `5xx`; defer the item after exhaustion. Authentication and configuration errors are task-fatal.
- Block new requests from one hour before through one hour after Pacific reset. Owner overrides apply only to the active window, persist until it ends, and emit an auditable event.

## Startup checklist

1. Read this file and the nearest nested `AGENTS.md` files for touched paths.
2. Preserve monorepo-first modular boundaries.
3. Convert requirements into concrete modules, task definitions, and small deliverable slices.
4. Avoid infrastructure not justified by current requirements.
5. Verify dependency and runtime assumptions for embedded flows.

If guidance diverges, the nearest applicable `AGENTS.md` is authoritative; update older notes to match.
