# Verification

Create, modify, or run tests only when the owner explicitly requests that work, as required by root [AGENTS.md](../AGENTS.md). Implementation/refactoring and committing do not imply test authorization. No automatic TDD or full-suite requirement applies.

## Default inspection

Review changed code and contracts, inspect relevant paths/configuration, and use `git diff --check`. For documentation, verify local links and owner paths against retained code. Do not start the backend or apply migrations merely to validate docs. Report the limits of inspection; do not claim runtime readiness from static checks.

## Coverage available on request

`tests/test_api_assembly.py` is retained but obsolete: its API factory was removed with the owner-authorized HTTP retirement. It has not been modified or run. There is no CLI or catalog integration suite; pytest is not a runtime dependency. Replacing this test requires an explicit request for test work.

Static CLI validation includes syntax inspection, retained import/reference checks, dependency inspection, and `git diff --check`. It does not establish terminal interaction, live provider behavior, transactional catalog mutations, checkpoint recovery, or safe-stop readiness.

Terminal verification requires explicit owner authorization. The current CLI acceptance scope is:

1. Launch idle with the requested task/options selected; task selection never starts work.
2. Use command/task/history pickers, settings validation, disabled tasks, and narrow-terminal resizing without persistent clutter.
3. Reject repeated starts and task/settings changes from the initial request through worker finalization and output drain.
4. Stream every redacted run message into native scrollback while preserving partially typed commands and responsive controls.
5. Distinguish starting, discovery, processing, provider backoff, safe stopping, and finalizing; animate quiet activity without fabricating progress, and visibly invalidate progress after runtime-read failure.
6. Browse history without changing foreground identity; distinguish deferred, stopped, failed, and completed results.
7. Safely stop with Ctrl-C, drain output, return to idle, and start another run.
8. Safely quit or force exit during startup/work/finalization with terminal restoration and retained recovery semantics; verify idle input clearing and ordinary `q` handling.
9. Exercise slow/broken output without silent loss during normal operation, unbounded buffering, deadlock, or false success. With the terminal reader stalled, input must remain attached and a repeated Ctrl-C must exit with code 130 without waiting for output. Resume slow output and inspect transcript ordering, coalesced redraws, resize handling, and final drain. Output errors, including during final renderer cleanup, must produce exit code 1.
10. Create structured artifacts/events and saved summaries without an interactive `.log` file or dependence on an existing task directory.
11. Retain existing summaries/log files and daily maintenance file/console output, flushing, and worker-finalization behavior.

These scenarios are pending runtime acceptance coverage, not an instruction to execute them during ordinary implementation.

If the owner requests database tests, use an isolated PostgreSQL instance matching the migrated catalog with explicit test-only configuration. Never fall back to the owner's database or local config. Keep test scope within the request.

Credential-backed Gemini/storage/converter smoke testing also requires an explicit request, configured services, and reviewed cohorts. Backup restore drills remain deliberate operations under the recovery procedure.
