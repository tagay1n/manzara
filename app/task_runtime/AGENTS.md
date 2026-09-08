# Task runtime guidance

These rules apply to `app/task_runtime/`. Shared contracts also cover `app/tasks.py`, `app/run_artifact_channel.py`, `app/run_artifacts.py`, and `app/run_summary.py`.

- Every run writes `~/.manzara/logs/task-runs/<task_id>/run-<run-id>.log`, or the equivalent configured artifacts-root path. This file is the authoritative verbose run log.
- Use the shared structured line format with timestamp, level, run/task/panel/source context, and message.
- Do not persist stdout/stderr or `task.log` events in either database. Serve bounded log pages from the artifact file using run-local cursors.
- Coalesce `task.progress` persistence in local SQLite and remove transient progress events at the terminal run boundary. Keep the latest progress snapshot on the local `runs` row.
- Log start, per-item, decision, failure, and final-summary boundaries. Include stable identifiers for successful mutations.
- Never derive structured artifacts by parsing logs. Emit compact `task.artifact` SSE payloads into local SQLite and expose large details through paginated endpoints.
- Preserve graceful stop boundaries, restartable checkpoints, redaction, and actionable error context in run state, logs, and events.
- Use `after_log_id` for follow and `before_log_id` for backfill. Keep reads bounded.
