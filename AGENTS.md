# AGENTS.md

Last updated: 2026-03-25  
Owner: tans1q

## Purpose
This repository is a **single monorepo** for Tatar-language content operations and data workflows.

The immediate goal is to replace manual CLI-heavy operations with a maintainable operational system (and later a web UI).

## Product Intent
Support end-to-end workflows for:
- video content
- text from web pages
- document files
- other source artifacts

## Confirmed Decisions
- Monorepo-first approach is intentional.
- Splitting into multiple repositories may happen later if complexity grows too much.
- Architecture should remain modular even inside one monorepo.
- Requirements are still being discovered iteratively.

## Pipeline Model
Typical stages:
1. Ingest content from sources.
2. Store raw/collected files in Yandex Disk and/or S3-compatible storage.
3. Optionally enrich metadata (including Gemini-assisted extraction).
4. Process/transform content.
5. Distribute selected outputs (for example torrents, other object storage).
6. Assemble datasets and publish to Hugging Face.

Notes:
- Workflows are conditional; not every item passes every stage.
- Stages should be composable and independently runnable.

## Engineering Constraints
- Keep complexity controlled and architecture understandable.
- Prefer explicit module boundaries over ad-hoc scripts.
- Prioritize operational visibility (run state, logs, artifacts, failures).
- Runtime state store is PostgreSQL only (`MANZARA_DATABASE_URL`) in schema `monocorpus` by default (`MANZARA_DB_SCHEMA`); do not reintroduce SQLite runtime paths.
- Known temporary exception: Oscar stage runner still reads legacy `state.sqlite` for snapshot queue seeding; keep changes isolated and plan full PostgreSQL cutover cleanup later.
- Shayan state (download manifest + snapshot history) is PostgreSQL-backed. Do not use persistent `status.json` / `latest.json` as runtime source of truth.
- Artifact location rule (all flows/tasks): write all task/flow artifacts (logs, temp files, exports, caches, run metadata) only under `~/.manzara` by default (or under `MANZARA_ARTIFACTS_ROOT` when explicitly overridden). Do not write artifacts into repository-root folders such as `_artifacts`.
- Keep secrets out of git: treat `config.yaml` as local-only and maintain masked `config.example.yaml` in sync with config structure changes.
- Runtime loaders must not use `config.example.yaml` as an input source; it is reference-only.
- Keep a single dependency file policy (`requirements.txt`) unless owner explicitly asks to split.
- When copying/adjusting embedded runtime code, update dependencies in `requirements.txt` for any new external imports.
- For runtime-heavy tasks (for example Library `meta evaluate`), keep automated coverage where practical and record manual smoke-test expectations in README when full E2E is not in tests.
- Frontend work should follow TDD where applicable:
  - Add/adjust tests first for frontend behavior that is testable and meaningful.
  - Skip forced tests for purely cosmetic or low-value visual tweaks when tests would be brittle; document manual verification instead.
- Backend is the source of truth for business/data state.
- Do not duplicate business state or decision logic on frontend when backend already owns it.
- Keep frontend thin ("stupid frontend"): focus on rendering, interaction, and transport of user intent; keep domain decisions on backend.
- Frontend-local state is acceptable only for UI concerns (for example view toggles, transient input, optimistic UX markers), not as an alternate domain truth.

## Frontend Standards
- UI reference baseline: https://github.com/builderz-labs/mission-control
  - Treat it as visual/UX direction (console layout, density, hierarchy, panel style), while keeping Manzara-specific information architecture and behavior.
- Treat backend API/event contracts as authoritative; align frontend types and adapters to backend schemas.
- Bootstrap page state with API on initial load, then apply important runtime state transitions from SSE events.
- Route all HTTP calls through a shared client layer (timeouts, retries, auth headers, error normalization).
- Model view states explicitly: `loading`, `ready`, `empty`, `error`.
- Keep server-driven state synchronized from API/SSE; avoid shadow copies of domain truth on frontend.
- Use optimistic UI only when needed and always reconcile/rollback from backend-confirmed state.
- Keep task actions idempotent from UX perspective (safe re-click behavior, disabled/guarded pending states).
- Prefer behavior-focused frontend tests (user flows, state transitions, API mapping) over brittle visual snapshots.
- Document manual verification for purely visual/cosmetic changes when automated tests add low value.
- Enforce accessibility baseline (keyboard navigation, visible focus, semantic structure, label/contrast checks).
- Keep rendering safe by default; do not inject unsanitized HTML.
- Reuse design tokens/components for consistency across pages; avoid one-off styling drift.
- Add lightweight client observability for failures with route/task/run context where possible.
- Task/run log viewing must use one shared behavior across the app (not per-page custom logic):
  - On open, load only the latest `N` lines (tail behavior), not full history.
  - Keep appending new lines from backend as they arrive (follow behavior).
  - On upward scroll near the top, load previous `N` lines and prepend while preserving viewport position.
  - Use cursor-based pagination (for example `before_log_id` / `after_log_id`) instead of offset pagination for large logs.
  - Keep the log viewer implementation centralized and reused by all task pages/components.
- Routing/UI shape for operations must stay consistent:
  - Flow pages (`/flows/{slug}`) show flow-level stats + all flow tasks.
  - Task pages (`/tasks/{slug}`) own per-task history (newest first, default page-size 20 unless explicitly overridden by product requirement).
  - Run history cards should render backend-provided structured summaries when available.
- Use European date/time presentation in UI by default:
  - 24-hour clock (`HH:mm`, no AM/PM).
  - Day-first date order (`DD.MM.YYYY` where a concrete date string is shown).
  - Keep timezone explicit on operational timestamps when ambiguity is possible.

## Backend Standards
- Backend changes are TDD-first by default:
  - Write or adjust failing tests first for behavior changes, then implement, then refactor.
  - If full automation is impractical (external/runtime-heavy dependencies), add focused unit/integration coverage for core logic and document the exact manual smoke checks in `README`.
- Keep backend as the only source of domain truth; do not move business decisions to frontend.
- Prefer explicit contracts for API/SSE payloads and backward-compatible schema changes.
- Validate external API payloads strictly and fail fast with actionable `400` errors:
  - Avoid silent coercion of ambiguous values.
  - For boolean-like control fields, accept only explicit allowlisted forms.
  - For integer control fields, require integral values (no implicit truncation from floats).
- Keep flow modules isolated (`app/modules/<flow>/...`) with clear ownership boundaries.
- No silent failures: surface actionable error context in run state, logs, and SSE events.
- Task lifecycle requirements apply to all tasks:
  - Every task must support graceful shutdown on stop request (finish current safe boundary, persist state, then exit cleanly).
  - Every task must be resumable after interruption/restart; progress/state checkpoints must allow continuing without starting from scratch.
- Logging/observability is mandatory for task execution paths:
  - Every task run must have a dedicated artifact log file under `~/.manzara/task_runs/<task_id>/run-<run_id>.log` (or `MANZARA_ARTIFACTS_ROOT/task_runs/...` when overridden).
  - Use one uniform structured line format for runtime-emitted lines: timestamp, level, run/task/panel/source context, message.
  - Persist user-visible stdout/stderr lines to DB logs and mirror them into artifact run logs with context metadata.
  - Include explicit start/final status lines in runtime logs so long-running task outcomes are auditable offline.
  - Keep secrets out of logs (mask/redact credentials and tokens).
  - Log API access patterns must support efficient tail/follow UX at scale:
    - Cursor-based forward reads for live follow (`after_log_id`).
    - Cursor-based backward reads for history backfill (`before_log_id`).
    - Bounded batch size (`limit`) for both directions.
- Gemini usage must be centralized behind a shared runtime manager (no per-task ad-hoc key picking):
  - Treat Gemini keys as grouped by `account -> keys[]` from config.
  - Do not hardcode model names in task logic; resolve model aliases from config (`gemini.models`), while quota/runtime state stays per `(account, key, model)`.
  - Persist Gemini runtime state in PostgreSQL (not artifact files) so restarts keep continuity.
  - Key exhaustion is model-scoped only; a key exhausted for one model can still be used for others.
  - Per-key minute throttle: at most one request per minute per `(account, key, model)`.
  - Selection policy: random account first, then random key in that account; if selected key is cooling down, try another key before waiting.
  - Daily limits are inferred from Gemini responses (no local fixed RPD enforcement).
  - On Gemini `429`: log full payload/context and fail current task run; parsing subtypes can be improved incrementally from observed payloads.
  - On Gemini `400`: treat as request-level rejection (prompt/input issue); do not exhaust or pause keys, skip/fail only the current item and continue workflow processing.
  - On Gemini `5xx`: start a global Gemini pause for 60 seconds and block new Gemini calls during pause.
  - Enforce Gemini reset blackout window around Pacific reset:
    - No new Gemini calls from 1 hour before to 1 hour after reset.
    - In-flight requests may finish gracefully.
  - At daily reset rollover, clear exhausted markers for all keys.

## Low-Context Scalability Rules
These rules apply to both backend and frontend to keep implementation understandable as flows/tasks/pages grow.

Backend:
- Prefer declarative registries/maps over `if/elif` routing for flow/task dispatch.
- Keep task execution on a shared contract (`prepare`, `run`, `validate`, `summarize`) where practical.
- Move skip/retry/overlap/schedule behavior into policy config tables instead of inline condition chains.
- Use a single shared state-machine definition for run/workflow statuses and valid transitions.
- Keep business logic in small pure functions with typed inputs/outputs; keep side effects at module edges.
- Enforce strict module boundaries by flow (`app/modules/<flow>/...`); avoid cross-flow imports except shared core.
- Prefer additive extension points (register new handler) over editing central branching logic.
- Keep per-function complexity bounded (small functions, shallow nesting, early returns).

Frontend:
- Use route/page registries and config-driven rendering for repeated page patterns; avoid per-page custom branching where possible.
- Treat backend API/SSE payloads as single source of truth; do not duplicate domain decision logic client-side.
- Centralize data fetching, event handling, and error normalization in shared utilities.
- Use explicit view-state models (`loading`, `ready`, `empty`, `error`) instead of scattered boolean flags.
- Reuse shared components/tokens for cards, tables, tabs, forms, and task controls; avoid one-off UI logic.
- Keep local state UI-only (selection, expanded/collapsed, transient inputs); derive domain state from backend responses/events.
- Prefer declarative action mapping (`action_id -> handler`) over long click-handler condition chains.
- Add tests for shared behavior contracts (state transitions, API mapping, SSE reducers) so new pages reuse proven primitives.

## Agent Startup Checklist
When starting a new session in this repo:
1. Read this file first.
2. Preserve monorepo-first + modular architecture direction unless explicitly changed by owner.
3. Convert new requirements into concrete modules, task definitions, and MVP slices.
4. Avoid introducing heavy architecture before requirements justify it.
5. Verify dependency/runtime assumptions for embedded flows before shipping changes.

## Source of Truth
If this file and other notes diverge, treat `AGENTS.md` as the current guidance file and update others to match.
