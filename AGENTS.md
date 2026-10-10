# Repository rules

Owner: tans1q

## Communication and routing

- Use English with the owner unless another language is explicitly requested. Never use Russian in owner-facing replies, UI copy, artifacts, or summaries without an explicit request.
- Manzara is a monorepo for Tatar-language content operations. Flows live in `app/modules/<flow>/`; shared backend code lives in `app/`. Flows may import shared core; shared core must not import flow internals. Cross-flow imports go through shared core.
- The web frontend and HTTP APIs are removed. The inline operations CLI supports personality normalization, non-PDF extraction, book preview generation, static Library export, publisher clustering proposals, collection discovery proposals, metadata extraction/evaluation, and explicit cleanup review commands. Cleanup preparation and Yandex catalog sync run through `scripts/run_daily_maintenance.py` and the daily Actions workflow. Backblaze document transfer is a separate GitHub workflow and is absent from the CLI; every interactive task requires a Python handler. Repository code is limited to the operations CLI, GitHub workflow dependencies, and the separately invoked Alembic bootstrap. PostgreSQL catalog migration is complete according to the owner. Static inspection does not establish operational readiness.
- Read [docs/architecture.md](docs/architecture.md) to locate owners, then only matching guidance. Before editing, read the nearest `AGENTS.md`. For `app/gemini_*.py`, also read [docs/gemini-runtime.md](docs/gemini-runtime.md).

## Invariants

- Durable domain data and safety-critical workflow checkpoints use PostgreSQL (YAML `database_url` and `database_schema`). Runs, Gemini coordination, flow attempts/errors, AI retry exclusions, and reproducible caches use only local SQLite (YAML `local_state_path`). Task registrations are code-owned; dashboard definitions and conveyor state are retired. Never fall back between stores or dual-write.
- The backend owns domain decisions and persisted truth. Clients own rendering, transport, interaction, and transient state.
- Artifacts live under the YAML `artifacts_root`; never create repository-root runtime artifact directories.
- Keep secrets out of git and logs. Local configuration is gitignored; keep `config.example.yaml` masked and structurally current, and never load it at runtime. Operational values and processing policies come only from the selected YAML file; missing settings fail. `MANZARA_CONFIG_PATH` selects that file, without environment value overrides.
- Keep `requirements.txt` as the single dependency file.
- Prefer forward changes over compatibility branches. Ask the owner before choosing a persisted-data migration or compatibility policy.

## Engineering

- Create, modify, or run tests only when the owner explicitly requests that test work. Ordinary implementation, fixes, refactoring, documentation, and commits do not authorize tests or require TDD/full-suite runs. Use code review and appropriate non-test inspection by default; report validation limits. See [docs/verification.md](docs/verification.md).
- Validate external control payloads strictly: explicit boolean allowlists; integral integers without truncation.
- Prefer declarative registries and shared contracts; small functions, shallow nesting, side effects at boundaries.
- Define shared workflow states once. Tasks emit no events; progress and provider waits use the local run row. HTTP/SSE transport is retired.
- Tasks stop at safe boundaries, resume from persisted checkpoints, surface actionable failures, and retain structured result artifacts.
- All task logs go only to stdout using the shared formatter. Tasks process items sequentially with one task worker; necessary support threads are allowed. Save structured artifacts directly and reference them in run summaries, never through events or log parsing.
- Deliver small slices, verify runtime/dependency assumptions, and update stale guidance in the same change. Nearest instructions win.
- Document current contracts, operations, and unresolved work once. Use code for implementation detail and git history for completed handoffs; avoid duplicated setup, frozen test counts, benchmark snapshots, and nonexistent path references.
