# AGENTS.md

Last updated: 2026-03-24  
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
- Keep secrets out of git: treat `config.yaml` as local-only and maintain masked `config.example.yaml` in sync with config structure changes.
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

## Backend Standards
- Backend changes are TDD-first by default:
  - Write or adjust failing tests first for behavior changes, then implement, then refactor.
  - If full automation is impractical (external/runtime-heavy dependencies), add focused unit/integration coverage for core logic and document the exact manual smoke checks in `README`.
- Keep backend as the only source of domain truth; do not move business decisions to frontend.
- Prefer explicit contracts for API/SSE payloads and backward-compatible schema changes.
- Keep flow modules isolated (`app/modules/<flow>/...`) with clear ownership boundaries.
- No silent failures: surface actionable error context in run state, logs, and SSE events.
- Logging/observability is mandatory for task execution paths:
  - Every task run must have a dedicated artifact log file under `_artifacts/task_runs/<task_id>/run-<run_id>.log`.
  - Use one uniform structured line format for runtime-emitted lines: timestamp, level, run/task/panel/source context, message.
  - Persist user-visible stdout/stderr lines to DB logs and mirror them into artifact run logs with context metadata.
  - Include explicit start/final status lines in runtime logs so long-running task outcomes are auditable offline.
  - Keep secrets out of logs (mask/redact credentials and tokens).

## Agent Startup Checklist
When starting a new session in this repo:
1. Read this file first.
2. Preserve monorepo-first + modular architecture direction unless explicitly changed by owner.
3. Convert new requirements into concrete modules, task definitions, and MVP slices.
4. Avoid introducing heavy architecture before requirements justify it.
5. Verify dependency/runtime assumptions for embedded flows before shipping changes.

## Source of Truth
If this file and other notes diverge, treat `AGENTS.md` as the current guidance file and update others to match.
