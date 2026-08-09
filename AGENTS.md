# AGENTS.md

Last updated: 2026-08-04
Owner: tans1q

## Purpose
This repository is a **single monorepo** for Tatar-language content operations and data workflows.

The immediate goal is to replace manual CLI-heavy operations with a maintainable operational system and web console.

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
- Shayan state (download manifest + snapshot history) is PostgreSQL-backed. Do not use persistent `status.json` / `latest.json` as runtime source of truth.
- Shayan video archive policy:
  - Nextcloud WebDAV is the target archive for `shayan.transfer_yadisk_webdav`; Yandex Disk remains a retained source after each exact video version is verified remotely.
  - The copy task is non-destructive: never delete, trash, or move source videos on Yandex Disk after copying them to Nextcloud.
  - Use Nextcloud chunked upload v2 for video-sized files, assemble into a deterministic temporary DAV path, independently stream-hash the uploaded bytes, then move to the final path.
  - Keep Nextcloud staging components short and MD5-based (`.manzara-<md5>.uploading`). Do not use a `.part` suffix: Hetzner Storage Share returns a server-side `500 TypeError` when probing those paths.
  - Emit bounded chunk-level byte progress through the shared `task.progress` SSE contract; do not derive transfer progress from logs.
  - Persist WebDAV ETag/checksum checkpoints in PostgreSQL and reuse verified final or temporary uploads after restart.
  - Run one logged WebDAV preflight before source discovery. Authentication errors fail once with actionable context; rate limits retry the same request with interruptible backoff rather than failing successive files. Read-only WebDAV probes retry transient server errors at the same safe boundary.
- Document storage policy:
  - Backblaze B2 through its S3-compatible API is the primary document store; configure it only under `documents.primary_storage`. Keep `yandex.cloud` as legacy document/upstream/preview storage rather than repointing unrelated Yandex consumers.
  - Yandex Disk is an auxiliary ingest/provenance source. Document sync must never publish, delete, trash, or move Yandex Disk documents.
  - `maintenance.sync_documents_s3` discovers from the configured Yandex root and uses bytes in this order: hash-valid local cache, verified Backblaze object, verified legacy Yandex S3 object, Yandex Disk.
  - Document discovery and transfer form one sequential streaming pipeline: process each first-seen MD5 immediately, use per-object Backblaze checks, and never wait for a complete Yandex or bucket inventory before useful work begins.
  - Every newly uploaded Backblaze object must be downloaded and content-hashed before its PostgreSQL verification checkpoint is committed; size or client-written metadata alone is not sufficient proof.
  - Upload progress must use boto3 callbacks and the shared `task.progress` SSE contract. Graceful stop finishes the current document and exits at the next document boundary.
  - Before retrying an interrupted object, abort unfinished multipart uploads for that exact content-addressed key; never treat an incomplete multipart upload as resumable state.
  - Document sync treats the legacy document cache as read-only input and never treats cache-only files as discovered documents. Other Library cache behavior remains governed by the shared-cache rule below.
  - Restricted documents belong in the configured private bucket and must be accessed through backend-generated short-lived signed URLs.
  - Persist S3 size/ETag/verification timestamps in PostgreSQL; do not re-download or re-hash verified unchanged objects on every run.
  - The legacy `public.document` table currently has no database uniqueness constraint on `md5`. Treat MD5 as the application identity: reject duplicate/null identities before remote work, persist with transactional update-then-insert, and roll back if an update matches more than one row. Do not add or alter its constraint without explicit owner approval.
  - Every completed document sync must emit a structured source/database reconciliation report: source file count, canonical source document count, database rows before/after, synced/unsynced source documents, database-only rows, duplicate source paths, item failures, and a `fully_synced` result.
  - Every document-related Yandex Disk move or removal must be represented by a PostgreSQL `document_cleanup_queue` row before mutation. Task code must use the guarded cleanup executor rather than calling Yandex move/remove directly.
  - `library.prepare_document_cleanup` is planning-only: it may identify non-Tatar/non-document records and duplicate ISBNs, but must not mutate documents or remote storage. Ambiguous ISBN groups remain review rows.
  - `maintenance.monocorpus_sync` applies persisted cleanup rows and synchronizes Yandex catalog state. Duplicate-MD5 resources may be queued and executed in the same traversal; ordinary unrestricted documents missing a public URL may be published, while restricted documents must never be published.
  - A document cleanup move is complete only after the filtered-out target is MD5-verified, managed S3 derivatives are removed, and dependent PostgreSQL state is deleted. Each phase must be resumable and idempotent.
  - `maintenance.monocorpus_sync` and `maintenance.sync_documents_s3` must share one PostgreSQL advisory lock and never overlap.
  - Exact reconciliation is valid only after Yandex traversal finishes. A stopped partial traversal must report `discovery_complete=false`, leave database-only counts unevaluated, and never claim `fully_synced`.
  - Reconciliation differences are reportable outcomes, not process failures. Complete the task with differences so the web UI can show final statistics; still fail normally when discovery or another task-level error prevents producing a trustworthy final report.
- Artifact location rule (all flows/tasks): write all task/flow artifacts (logs, temp files, exports, caches, run metadata) only under `~/.manzara` by default (or under `MANZARA_ARTIFACTS_ROOT` when explicitly overridden). Do not write artifacts into repository-root folders such as `_artifacts`.
- The existing `~/.monocorpus/0_entry_point` directory is a shared persistent source-document cache, not a Manzara task-artifact location. Library tasks may verify, reuse, and populate that cache; generated outputs and temporary files still belong under `~/.manzara`.
- Keep secrets out of git: treat `config.yaml` as local-only and maintain masked `config.example.yaml` in sync with config structure changes.
- Runtime loaders must not use `config.example.yaml` as an input source; it is reference-only.
- Keep a single dependency file policy (`requirements.txt`) unless owner explicitly asks to split.
- When copying/adjusting embedded runtime code, update dependencies in `requirements.txt` for any new external imports.
- Do not preserve code-level backward compatibility by default:
  - Prefer clean forward changes over compatibility branches/legacy config fallbacks.
  - Backward compatibility is required by default only for persisted database data.
  - For DB/data compatibility decisions, ask owner explicitly whether compatibility/migration is required.
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
- Keep default operational surfaces calm:
  - Reserve saturated color, animation, and high-contrast treatment for active work, warnings, failures, and destructive actions.
  - Do not keep healthy/idle controls visually highlighted when they do not require attention.
- Keep the global shell shared across pages:
  - Primary navigation, command palette, connection state, global task state, dialogs, toasts, and footer behavior belong in shared shell/UI modules.
  - Page scripts own page-specific rendering and intent only; do not fork shell behavior per page.
- Keep browser assets deterministic and locally served where practical; do not depend on unversioned CDN resources for core navigation/icons.
- Treat backend API/event contracts as authoritative; align frontend types and adapters to backend schemas.
- Bootstrap page state with API on initial load, then apply important runtime state transitions from SSE events.
- Seed each page's SSE connection from that page's own API snapshot cursor:
  - Snapshot payloads must expose `event_cursor`, captured before composing snapshot state.
  - Start SSE with `after_event_id=event_cursor`; do not replay from `0` after loading current state.
  - Do not seed a page from another page or shell snapshot, because it can skip domain events not represented in the page state.
- Treat high-frequency SSE traffic carefully:
  - `task.log` events must never trigger broad page API reloads or full DOM rerenders.
  - Apply lightweight task lifecycle/progress changes directly from SSE payloads where possible.
  - Reconcile only relevant terminal/artifact/configuration events, and coalesce overlapping refresh requests into one trailing refresh.
- For task artifacts/counters, UI live updates must come from explicit SSE artifact events (not from parsing stdout/stderr logs).
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
- Do not use browser system dialogs (`window.alert`, `window.confirm`, `window.prompt`) for user-facing UX flows.
  - Use custom in-app modal dialogs aligned with Manzara visual style and interaction patterns.
- Add lightweight client observability for failures with route/task/run context where possible.
- Task/run log viewing must use one shared behavior across the app (not per-page custom logic):
  - On open, load only the latest `N` lines (tail behavior), not full history.
  - Keep appending new lines from backend as they arrive (follow behavior).
  - On upward scroll near the top, load previous `N` lines and prepend while preserving viewport position.
  - Use cursor-based pagination (for example `before_log_id` / `after_log_id`) instead of offset pagination for large logs.
  - Keep the log viewer implementation centralized and reused by all task pages/components.
- Keep artifact presentation split by purpose:
  - Live task cards/history counters should use compact SSE payloads (for example added/changed/removed counts).
  - Detailed per-item diffs/lists should come from dedicated backend endpoints backed by PostgreSQL tables.
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
  - Task implementations must emit transparent progress logs at meaningful boundaries (start, per-item processing for batch jobs, success/failure decisions, final summary).
  - For data-mutating batch tasks, log each successful item with stable identifiers (for example entry id/path) so results are auditable without parsing external systems.
  - Include explicit start/final status lines in runtime logs so long-running task outcomes are auditable offline.
  - Keep secrets out of logs (mask/redact credentials and tokens).
  - Log API access patterns must support efficient tail/follow UX at scale:
    - Cursor-based forward reads for live follow (`after_log_id`).
    - Cursor-based backward reads for history backfill (`before_log_id`).
    - Bounded batch size (`limit`) for both directions.
- Task artifact contract (all flows/tasks):
  - Do not use stdout/stderr log parsing as the source of truth for structured run artifacts.
  - Emit structured artifact events explicitly (SSE event type such as `task.artifact`) with minimal JSON payload required for live UX.
  - Persist artifact payloads in PostgreSQL and use them when building run summaries/history cards.
  - For large detail payloads, persist normalized rows in dedicated tables and expose API endpoints for paginated drill-down.
  - Shayan tasks must follow this event-first artifact pattern (live counters via SSE, details via DB/API).
- Library PDF preview state is PostgreSQL-backed and variable by source length:
  - One-page PDFs have first-page previews; two-page PDFs have first/last; longer PDFs have first/second/last.
  - Missing semantic roles for short PDFs are intentional completeness, not partial failure; never create duplicate page previews.
  - Frontend/API consumers use manifest roles and actual page numbers, never infer semantics from compact S3 filenames.
- Library collection detection is proposal-based and path-independent:
  - Collection detection, Gemini validation, and explicit metadata application tasks belong to the dedicated `collections` flow; keep general Library operations in `library`.
  - Canonical collections and accepted memberships are authoritative; detector and Gemini reruns must never mutate them directly.
  - Detection eligibility requires an object-valued `metadata.schema_org` with a usable title. Exclude `@type=Legislation` and centralized normalized legal-genre policy matches before feature indexing.
  - Never use Yandex paths, parent directories, filenames, or storage hierarchy as collection evidence or Gemini prompt input.
  - Match new records against approved collection signatures first, then create new-collection proposals from coherent unmatched groups.
  - A document has at most one canonical collection membership. Conflicts require explicit owner resolution and must never silently move a document.
  - Discovery writes resumable proposal rows; Gemini validation writes advisory verdicts; only an explicit owner approval transaction creates collections or memberships.
  - New collections require at least two approved documents. Attachment proposals to an existing collection may contain one document.
  - Keep `library.collection_apply` separate from proposal approval; applying collection metadata is an independent explicit operation.
- Gemini usage must be centralized behind a shared runtime manager (no per-task ad-hoc key picking):
  - Treat Gemini keys as grouped by `account -> keys[]` from config.
  - Do not hardcode model names in task logic; resolve model aliases from config (`gemini.models`), while quota/runtime state stays per `(account, key, model)`.
  - Persist Gemini runtime state in PostgreSQL (not artifact files) so restarts keep continuity.
  - Key exhaustion is model-scoped only; a key exhausted for one model can still be used for others.
  - Per-key minute throttle: at most one request per minute per `(account, key, model)`.
  - Selection policy: random account first, then random key in that account; if selected key is cooling down, try another key before waiting.
  - Daily limits are inferred from Gemini responses (no local fixed RPD enforcement).
  - On Gemini `429`: log full payload/context and fail the current task by default. Explicit model-pool workflows may persist the failed attempt and continue through another configured model; never implement this rotation ad hoc inside task business logic.
  - On Gemini `400`: treat as request-level rejection (prompt/input issue); do not exhaust or pause keys, skip/fail only the current item and continue workflow processing.
  - On Gemini `5xx`: start a global Gemini pause for 60 seconds and block new Gemini calls during pause.
  - Enforce Gemini reset blackout window around Pacific reset:
    - No new Gemini calls from 1 hour before to 1 hour after reset.
    - In-flight requests may finish gracefully.
  - At daily reset rollover, clear exhausted markers for all keys.
  - Collection validation uses `gemini.model_pools.library_collection_validation`, one model verdict per batch, and no consensus voting.
  - Collection validation batches are adaptive per model: start at or below 20, retry timeout/malformed output twice, then reduce through `20 -> 10 -> 5 -> 2 -> 1`; `400`, `429`, blackout, and `5xx` do not change batch size.
  - Strictly validate collection responses against the requested MD5 set. Missing, duplicated, unknown, or malformed item results are response failures, not successful checkpoints.
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
