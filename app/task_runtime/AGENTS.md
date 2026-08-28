# Task runtime guidance

These rules apply to `app/task_runtime/`. Shared contracts also cover `app/tasks.py`, `app/run_artifact_channel.py`, `app/run_artifacts.py`, and `app/run_summary.py`.

- Every run writes `~/.manzara/task_runs/<task_id>/run-<run_id>.log`, or the equivalent configured artifacts-root path.
- Use the shared structured line format with timestamp, level, run/task/panel/source context, and message. Persist visible stdout/stderr in PostgreSQL and mirror it to the artifact log.
- Log start, per-item, decision, failure, and final-summary boundaries. Include stable identifiers for successful mutations.
- Never derive structured artifacts by parsing logs. Emit compact `task.artifact` SSE payloads, persist them, and expose large details through paginated endpoints.
- Preserve graceful stop boundaries, restartable checkpoints, redaction, and actionable error context in run state, logs, and events.
- Use `after_log_id` for follow and `before_log_id` for backfill. Keep reads bounded.
