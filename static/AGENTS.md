# Frontend guidance

These rules apply to `static/`.

- Use the mission-control project as visual direction while retaining Manzara information architecture.
- The shared shell owns navigation, connections, global task state, dialogs, toasts, logs, and footer behavior. Page scripts own page-specific rendering and intent.
- Route HTTP through `ManzaraCore`. Model `loading`, `ready`, `empty`, and `error` explicitly.
- Bootstrap each page from its own API snapshot. Seed SSE from that snapshot's `event_cursor`; never replay from zero or borrow another page's cursor.
- Apply frequent lifecycle/progress events directly. `task.log` must not trigger broad reloads or full rerenders; coalesce relevant terminal/artifact refreshes.
- Live counters come from SSE artifacts. Detailed lists come from PostgreSQL-backed endpoints.
- Reuse shared components and tokens, render untrusted values safely, serve deterministic local assets, and avoid browser `alert`, `confirm`, and `prompt`.
- Preserve semantic structure, keyboard navigation, visible focus, labels, and sufficient contrast.
- Shared task log viewers use tail/follow/backfill with cursor pagination.
- Flow pages show flow stats and tasks. Task pages own newest-first task history with a default page size of 20.
- Use 24-hour, day-first operational timestamps and show timezone where ambiguous.
