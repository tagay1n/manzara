# Manzara UI Control and State Spec (v0)

Date: 2026-03-21
Status: Draft, approved as design baseline

## 1) Dashboard Layout
- Top header (global controls, system status, user/profile area).
- Main content grid of operational panels.
- Footer (version/build, last event timestamp, quick links).

## 2) Panel Contract (example: Library)
Each panel must show:
- Source title (`Library`).
- Current task status (`Idle`, `Running`, `Stopping`, `Failed`, `Completed`).
- Key stat: documents total.
- Key stat: completed work in the latest run.
- Key stat: failures in the latest run.
- Key stat: last successful run timestamp.
- Key stat: last scan timestamp.
- Task control (icon-only): scan for changes.
- Task control (icon-only): download new.
- Task control (icon-only): show logs.

## 3) Icon Set and Glyph Mapping
Use one consistent icon library (recommended: Lucide).

Task buttons are icon-only, with tooltip and `aria-label` required.

- Scan for changes: `RefreshCw`
- Download new/start: `Play`
- Stop task (graceful): `Square`
- Force stop task: `OctagonX` (or `ShieldX` if preferred in chosen library)
- View logs: `Terminal`
- Running indicator: `LoaderCircle` (spinning)
- Completed marker: `CircleCheck`
- Failed marker: `CircleAlert`

No text labels inside task buttons.

## 4) Task Button Behavior
Per task button state:
- `Idle`: primary action icon (`Play` for download, `RefreshCw` for scan).
- `Starting`: disabled + spinner overlay.
- `Running`: icon switches to graceful stop (`Square`), progress bar visible.
- `StoppingGraceful`: disabled stop icon + spinner, progress bar amber.
- `ForceStopping`: disabled force icon + spinner, progress bar red.
- `Completed`: returns to idle icon after short success flash.
- `Failed`: returns to idle icon with panel error badge.

Single-click behavior:
- Idle task button click -> start task.
- Running task button click -> request graceful stop for that task.
- Second click while graceful stop pending -> force stop for that task.

## 5) Global Header Stop-All (Two-Step)
Header includes one global emergency control.

Behavior:
- If no running tasks: disabled.
- First press: send `STOP_ALL_GRACEFUL` event.
- Button switches to armed/force mode for active shutdown window.
- Second press (while tasks still running): send `STOP_ALL_FORCE` event.

Visual states:
- Normal: `Square` icon, neutral/amber style.
- Armed (after first press): `OctagonX` icon, red style.
- Cooldown/reset: returns to normal when no running tasks remain.

Safety:
- Confirmation is optional for first press (not required).
- Confirmation required for second press (force), unless user disables confirmations in settings.

## 6) Progress and Long-Running Tasks
- Tasks may run for hours/days.
- Progress bar mode: determinate when total units known (`done/total`).
- Progress bar mode: indeterminate with animation when total unknown.
- Show elapsed runtime and last heartbeat timestamp.
- Mark task `stale` if no heartbeat within configured threshold.

## 7) Logs UX
- Logs open in side drawer or modal from `Terminal` icon.
- Stream log lines live in append mode.
- Controls: pause autoscroll, copy, download raw log.
- Show run metadata at top: run id, source, started at, current status.

## 8) Event Stream Requirements
UI updates must be event-driven, near real-time.

Minimum event types:
- `task.started`
- `task.progress`
- `task.log`
- `task.stop_requested` (graceful)
- `task.force_stop_requested`
- `task.stopped`
- `task.completed`
- `task.failed`
- `system.stop_all_requested`

Transport recommendation (MVP):
- Server-Sent Events (SSE) from backend to UI.
- WebSocket can be added later if bidirectional low-latency control is needed.

## 9) Data Needed by UI (minimal)
- `tasks` table/view: current state, source, type, started_at, updated_at.
- `runs` table: historical executions, durations, outcomes.
- `task_events` table: status transitions and progress events.
- `task_logs` table/store: ordered log lines linked to run id.
- `source_stats` table/view: panel counters and last-run metrics.

## 10) Non-Negotiable UX Rules
- Every icon-only button must have tooltip and keyboard focus style.
- Every action that changes execution state must reflect in UI within seconds.
- Stop operations must be idempotent (double click does not create inconsistent state).
- Dashboard remains usable during long runs and reconnects.
