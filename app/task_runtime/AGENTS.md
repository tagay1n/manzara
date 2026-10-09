# Task runtime guidance

These rules apply to `app/task_runtime/`. Shared contracts also cover `app/tasks.py`, `app/run_artifact_channel.py`, `app/run_artifacts.py`, and `app/run_summary.py`.

- Every run writes `~/.manzara/logs/task-runs/<task_id>/run-<run-id>.log`, or the equivalent configured artifacts-root path. This file is the authoritative verbose run log.
- Use the shared structured line format with timestamp, level, run/task/panel/source context, and message.
- Do not persist stdout/stderr or `task.log` events in either database. Serve bounded log pages from the artifact file using run-local cursors.
- Coalesce `task.progress` persistence in local SQLite and remove transient progress events at the terminal run boundary. Keep the latest progress snapshot on the local `runs` row.
- Log start, per-item, decision, failure, and final-summary boundaries. Include stable identifiers for successful mutations.
- Never derive structured artifacts by parsing logs. Persist compact `task.artifact` events in local SQLite and save large details in run artifacts. The CLI reads bounded log pages; HTTP/SSE transport is retired.
- CLI tasks run in background threads with explicit run contexts and cooperative cancellation. Share one bounded PostgreSQL engine; never mutate per-task process environment or redirect global stdout. Propagate the run log context into worker threads.
- Noninteractive batches use the same `TaskRunner` and persisted task/artifact contracts. `batch.py` owns local session locking, recovery, sequential stages, signals, and cooperative time budgets; command boundaries supply flow descriptors and read-only preflight checks. Inspect terminal state only after worker finalization completes, and return nonzero for incomplete or failed runs.
- Batch console output follows the authoritative run log with bounded cursor pages and flushes final lines after worker exit. Periodic status snapshots are console-only; neither echoed logs nor snapshots create database log events or structured artifacts.
- CLI startup initializes only SQLite and checks the catalog read-only. PostgreSQL migrations remain a separate operation. Hold the local session lock before recovery and retain disabled-task history.
- Preserve graceful stop boundaries, restartable checkpoints, redaction, and actionable error context in run state, logs, and events.
- Use `after_log_id` for follow and `before_log_id` for backfill. Keep reads bounded.
