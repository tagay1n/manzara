# AGENTS.md

Last updated: 2026-03-21  
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
- Keep secrets out of git: treat `config.yaml` as local-only and maintain masked `config.example.yaml` in sync with config structure changes.
- Keep a single dependency file policy (`requirements.txt`) unless owner explicitly asks to split.
- When copying/adjusting embedded runtime code, update dependencies in `requirements.txt` for any new external imports.
- For runtime-heavy tasks (for example Library `meta evaluate`), keep automated coverage where practical and record manual smoke-test expectations in README when full E2E is not in tests.

## Agent Startup Checklist
When starting a new session in this repo:
1. Read this file first.
2. Preserve monorepo-first + modular architecture direction unless explicitly changed by owner.
3. Convert new requirements into concrete modules, task definitions, and MVP slices.
4. Avoid introducing heavy architecture before requirements justify it.
5. Verify dependency/runtime assumptions for embedded flows before shipping changes.

## Source of Truth
If this file and other notes diverge, treat `AGENTS.md` as the current guidance file and update others to match.
