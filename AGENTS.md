# Repository rules

Owner: tans1q

## Communication and routing

- Use English with the owner unless another language is explicitly requested. Never use Russian in owner-facing replies, UI copy, artifacts, or summaries without an explicit request.
- Manzara is a monorepo for Tatar-language content operations. Flows live in `app/modules/<flow>/`; shared backend code lives in `app/`. Flows may import shared core; shared core must not import flow internals. Cross-flow imports go through shared core.
- The web frontend and HTTP APIs are removed. The inline operations CLI supports personality normalization, cleanup preparation, and Yandex catalog sync; other tasks are disabled pending catalog adaptation. PostgreSQL catalog migration is complete according to the owner. Static inspection does not establish operational readiness.
- Read [docs/architecture.md](docs/architecture.md) to locate owners, then only matching guidance. Before editing, read the nearest `AGENTS.md`. For `app/gemini_*.py`, also read [docs/gemini-runtime.md](docs/gemini-runtime.md).

## Invariants

- Durable domain data and safety-critical workflow checkpoints use PostgreSQL (`MANZARA_DATABASE_URL`, schema `MANZARA_DB_SCHEMA`, default `monocorpus`). Definitions, runs, events, conveyor, Gemini coordination, flow attempts/errors, AI retry exclusions, and reproducible caches use only local SQLite (`~/.manzara/state/runtime.sqlite3` or `MANZARA_LOCAL_STATE_PATH`). Never fall back between stores or dual-write.
- The backend owns domain decisions and persisted truth. Clients own rendering, transport, interaction, and transient state.
- Artifacts live under `~/.manzara` or `MANZARA_ARTIFACTS_ROOT`; never create repository-root runtime artifact directories.
- Keep secrets out of git and logs. Local configuration is gitignored; keep `config.example.yaml` masked and structurally current, and never load it at runtime.
- Keep `requirements.txt` as the single dependency file.
- Prefer forward changes over compatibility branches. Ask the owner before choosing a persisted-data migration or compatibility policy.

## Engineering

- Create, modify, or run tests only when the owner explicitly requests that test work. Ordinary implementation, fixes, refactoring, documentation, and commits do not authorize tests or require TDD/full-suite runs. Use code review and appropriate non-test inspection by default; report validation limits. See [docs/verification.md](docs/verification.md).
- Validate external control payloads strictly: explicit boolean allowlists; integral integers without truncation.
- Prefer declarative registries and shared contracts; small functions, shallow nesting, side effects at boundaries.
- Define shared workflow states once. Preserve retained task/event payload schemas; HTTP/SSE transport has been retired by owner decision.
- Tasks stop at safe boundaries, resume from persisted checkpoints, surface actionable failures, and keep dedicated structured artifact logs.
- Structured artifacts require persisted `task.artifact` events, never log parsing. Log reads use bounded cursor pagination.
- Deliver small slices, verify runtime/dependency assumptions, and update stale guidance in the same change. Nearest instructions win.
- Document current contracts, operations, and unresolved work once. Use code for implementation detail and git history for completed handoffs; avoid duplicated setup, frozen test counts, benchmark snapshots, and nonexistent path references.
