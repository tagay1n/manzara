# Verification

Create, modify, or run tests only when the owner explicitly requests that work, as required by root [AGENTS.md](../AGENTS.md). Implementation/refactoring and committing do not imply test authorization. No automatic TDD or full-suite requirement applies.

## Default inspection

Review changed code and contracts, inspect relevant paths/configuration, and use `git diff --check`. For documentation, verify local links and owner paths against retained code. Do not start the backend or apply migrations merely to validate docs. Report the limits of inspection; do not claim runtime readiness from static checks.

## Coverage available on request

`tests/test_api_assembly.py` is retained but obsolete: its API factory was removed with the owner-authorized HTTP retirement. It has not been modified or run. There is no CLI or catalog integration suite; pytest is not a runtime dependency. Replacing this test requires an explicit request for test work.

Static CLI validation includes syntax inspection, retained import/reference checks, dependency inspection, and `git diff --check`. It does not establish terminal interaction, live provider behavior, transactional catalog mutations, checkpoint recovery, or safe-stop readiness.

If the owner requests database tests, use an isolated PostgreSQL instance matching the migrated catalog with explicit test-only configuration. Never fall back to the owner's database or local config. Keep test scope within the request.

Credential-backed Gemini/storage/converter smoke testing also requires an explicit request, configured services, and reviewed cohorts. Backup restore drills remain deliberate operations under the recovery procedure.
