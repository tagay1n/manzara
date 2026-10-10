# Task runtime rules

Applies to `app/task_runtime/` and `app/tasks.py`; extends root rules.

- `logging.py` owns shared formatting, redaction, run context, and flush/close. Never capture subprocess logs to files or add file-log switches. Persist coalesced progress/provider waits directly on local run rows.
- Save separate JSON results under artifact `workspaces/task-runs/<task_id>/run-<run_id>/` and reference them in summaries. Writers create directories independently of logging; detailed inventories stay in artifacts, not final log lines.
- One worker, sequential items; no executor pools/concurrent item queues/worker settings. `app/s3_transfer.py` disables SDK threads/CRT. Support threads may handle terminal I/O, heartbeat, leases, and subprocess transport.
- Handlers receive explicit `RunContext` logging/progress/artifacts/cancellation/options and share the bounded PostgreSQL engine. Never change per-task process environment or redirect global stdout. Log start/item/mutation/decision/failure/final boundaries with stable IDs.
- `batch.py` owns session locks, recovery, sequential stages, signals, and cooperative budgets. Composition supplies descriptors/read-only preflight. Finalize workers before inspecting terminal outcome; failed/stopped/deferred work returns nonzero.
- Batch stdout flushes each message. Interactive output redacts before a bounded ordered queue with backpressure and one terminal consumer. Renderer/transcript share a nonblocking writer; keep input attached and coalesce redraws.
- Output failure unblocks producers, requests safe stop, and exits 1. Keep the consumer alive during shutdown; never join workers on the UI loop. Force exit restores input without waiting on renderer flush/buffer locks.
- Enforce one active task through finalization; CLI foreground ownership extends through output drain. History must not replace foreground identity; presentation flags are not persisted states.
- Descriptors own Python handlers/grouping/full-inventory requirements. Reject invalid limits before creating runs; never persist definitions/worker counts. `panel_id` is a group identifier.
- Acquire the session lock before SQLite initialization/recovery; check PostgreSQL read-only. [Operations](../../docs/operations.md#local-runtime-state) owns the approved reset policy; migrations run separately. Preserve safe stops, checkpoint recovery, and actionable failures without legacy branches.
