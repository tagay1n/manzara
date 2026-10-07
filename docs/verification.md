# Verification

Create, modify, or run tests only when the owner explicitly requests that work, as required by root [AGENTS.md](../AGENTS.md). Implementation/refactoring and committing do not imply test authorization. No automatic TDD or full-suite requirement applies.

## Default inspection

Review changed code and contracts, inspect relevant paths/configuration, and use `git diff --check`. For documentation, verify local links and owner paths against retained code. Do not start the backend or apply migrations merely to validate docs. Report the limits of inspection; do not claim runtime readiness from static checks.

## Coverage available on request

This checkout retains `tests/test_api_assembly.py`, a database-free regression for API assembly after frontend removal. It uses `app.factory`, avoiding production configuration and startup migrations. Pytest is not listed in `requirements.txt`; a requested run needs an environment with it installed.

The former frontend/catalog/worker/architecture suites, shared fixtures, and Testcontainers setup are absent. No PostgreSQL test fixture currently exists. API assembly coverage does not establish catalog reads/writes, review protections, privacy, checkpoint recovery, or safe-stop behavior.

If the owner requests database tests, use an isolated PostgreSQL instance matching the migrated catalog with explicit test-only configuration. Never fall back to the owner's database or local config. Keep test scope within the request.

Credential-backed Gemini/storage/converter smoke testing also requires an explicit request, configured services, and reviewed cohorts. Backup restore drills remain deliberate operations under the recovery procedure.
