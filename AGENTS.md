# AGENTS.md

Last updated: 2026-08-28
Owner: tans1q

## Scope and routing

Manzara is the monorepo for Tatar-language content operations. Flow code belongs in `app/modules/<flow>/`; shared backend and frontend infrastructure belongs in `app/` and `static/`. Dependencies point inward: flows may import shared core, while shared core must not import flow internals.

Read `docs/architecture.md` for the ownership and focused-test index. Before changing a path, read its nearest `AGENTS.md`. For shared Gemini files (`app/gemini_*.py`) also read `docs/gemini-runtime.md`.

## Global invariants

- PostgreSQL is the only runtime state store (`MANZARA_DATABASE_URL`), using `monocorpus` by default (`MANZARA_DB_SCHEMA`). Do not add SQLite runtime paths.
- The backend owns business and persisted data truth. Frontend state is rendering, transport, interaction, and transient UI state only.
- Task artifacts live under `~/.manzara`, or `MANZARA_ARTIFACTS_ROOT` when explicitly configured. Never add repository-root runtime artifact directories.
- Keep secrets out of git and logs. `config.yaml` is local-only; keep masked `config.example.yaml` structurally current and never load it at runtime.
- Keep one dependency file, `requirements.txt`, unless the owner explicitly requests another.
- Prefer clean forward changes over compatibility branches. Persisted database compatibility is the exception: ask the owner before choosing a migration or compatibility policy.
- Preserve module ownership. Cross-flow imports are allowed only through shared core modules.

## Engineering standards

- Backend and meaningful frontend behavior changes are TDD-first: add or adjust a focused failing test, implement, refactor, then run focused tests. Run the full suite before commit. Cosmetic-only work may use documented manual verification.
- Validate external control payloads strictly. Boolean-like values use explicit allowlists; integer fields must be integral and never silently truncated.
- Prefer declarative registries and shared contracts over conditional routing. Keep functions small, nesting shallow, and side effects at module boundaries.
- Keep shared run/workflow states in one state-machine definition. Preserve backward-compatible API and SSE schema evolution.
- Every task must stop gracefully at a safe boundary, restart from persisted checkpoints, surface actionable failures, and retain its dedicated structured artifact log.
- Structured artifacts come from explicit persisted `task.artifact` events, never log parsing. Log reads use bounded cursor pagination.

## Change discipline

Convert requirements into small deliverable slices, avoid unjustified infrastructure, and verify runtime/dependency assumptions. If guidance conflicts, the nearest applicable `AGENTS.md` wins; update stale documentation in the same change.
