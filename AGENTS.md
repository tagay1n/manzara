# Repository rules

Owner: tans1q

## Routing

- Use English unless the owner explicitly requests another language. Never use Russian in replies, UI, artifacts, or summaries without that request.
- Read [architecture](docs/architecture.md) for owners, then only matching guidance and the nearest `AGENTS.md`. For `app/gemini_*.py`, also read [Gemini runtime](docs/gemini-runtime.md).
- Flows live in `app/modules/<flow>/`; shared core lives in `app/`. Flows may import core; core must not import flow internals. Cross-flow imports go through core.
- Retain only code reachable from the operations CLI, GitHub workflow runners, or separate Alembic bootstrap. Interactive tasks require Python handlers. [README](README.md) owns the task list; [operations](docs/operations.md) owns scheduled runners. Web/HTTP/SSE, dashboards, and conveyor state are retired.

## Invariants

- PostgreSQL (`database_url`, `database_schema`) owns domain data and safety-critical checkpoints. Local SQLite (`local_state_path`) owns runs, Gemini coordination, attempts/errors, AI exclusions, and reproducible caches. Never fall back between stores or dual-write. Task registrations are code-owned.
- Backend owns domain decisions and persisted truth; clients own rendering, transport, interaction, and transient state.
- Artifacts stay under YAML `artifacts_root`; never create repository-root runtime directories.
- Keep secrets out of git/logs. Selected private YAML is the sole settings source; `MANZARA_CONFIG_PATH` selects it without value overrides. Missing settings fail. Keep `config.example.yaml` masked/current and never load it at runtime. Actions provisioning combines `.github/config/` defaults with existing credentials into private YAML.
- `requirements.txt` is the sole dependency file. Prefer forward changes; ask before choosing a persisted-data migration or compatibility policy.

## Engineering

- Create, modify, or run tests only on explicit owner request. Implementation, documentation, refactoring, and commits do not authorize tests. Use review/non-test inspection and report limits; see [verification](docs/verification.md).
- Strictly validate external controls: explicit boolean allowlists and integral integers without truncation.
- Prefer declarative registries, shared contracts, small functions, shallow nesting, and boundary side effects. Define shared workflow states once.
- Tasks process items sequentially on one worker; necessary support threads are allowed. Stop at safe boundaries, resume checkpoints, and surface actionable failures.
- Use shared stdout-only logging. Tasks emit no events; progress/provider waits use local run rows. Save structured artifacts directly and link them in summaries; never parse logs for results.
- Deliver small slices, verify runtime/dependency assumptions, and update stale guidance. Nearest instructions win. Document contracts, operations, and unresolved work once; implementation belongs in code, completed handoffs in git history. Avoid frozen counts, benchmarks, and nonexistent paths.
