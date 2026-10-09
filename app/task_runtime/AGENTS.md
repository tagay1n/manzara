# Task runtime guidance

These rules apply to `app/task_runtime/`. Shared contracts also cover `app/tasks.py` and `app/run_artifact_channel.py`.

- Every run writes `~/.manzara/logs/task-runs/<task_id>/run-<run-id>.log`, or the equivalent configured artifacts-root path. This file is the authoritative verbose run log.
- Use the shared structured line format with timestamp, level, run/task/panel/source context, and message.
- Do not persist stdout/stderr or `task.log` events in either database. Serve bounded log pages from the artifact file using run-local cursors.
- Coalesce progress snapshots on the local `runs` row. Do not duplicate snapshots as `task.progress` events.
- Log start, per-item, decision, failure, and final-summary boundaries. Include stable identifiers for successful mutations.
- Never derive structured artifacts by parsing logs. Persist compact `task.artifact` events in local SQLite and save large details in run artifacts. The CLI reads bounded log pages; HTTP/SSE transport is retired.
- CLI tasks run in background threads with explicit run contexts and cooperative cancellation. Share one bounded PostgreSQL engine; never mutate per-task process environment or redirect global stdout. Propagate the run log context into worker threads.
- Noninteractive batches use the same `TaskRunner` and persisted task/artifact contracts. `batch.py` owns local session locking, recovery, sequential stages, signals, and cooperative time budgets; command boundaries supply flow descriptors and read-only preflight checks. Inspect terminal state only after worker finalization completes, and return nonzero for incomplete or failed runs.
- Batch logging sends each redacted structured line directly to stdout and its artifact file, flushing both immediately. The CLI uses only the file sink to avoid interfering with terminal rendering. Periodic batch status snapshots are console-only; console logs and snapshots do not create database log events or structured artifacts.
- Task descriptors own grouping, worker defaults, worker limits, and Python handlers. Do not persist task/panel definitions, icons, shell commands, or one-shot worker overrides. The retained `panel_id` run/event/log field identifies a task group; it has no dashboard definition or rendering owner.
- CLI startup initializes only SQLite and checks the catalog read-only. PostgreSQL migrations remain a separate operation. Hold the local session lock before recovery. Schema version 7 recreates older local runtime databases under the owner-approved fresh-state policy documented in `docs/operations.md`; subsequent startups retain run history and retry state.
- Preserve graceful stop boundaries, restartable checkpoints, redaction, and actionable error context in run state, logs, and events.
- Use `after_log_id` for follow and `before_log_id` for backfill. Keep reads bounded.
