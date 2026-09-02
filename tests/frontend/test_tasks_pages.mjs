import test from "node:test";
import assert from "node:assert/strict";
import {
  TASKS_PAGE_SOURCE, TASK_SOURCE, DASHBOARD_SOURCE, LIBRARY_SOURCE, DATABASE_SOURCE,
  GEMINI_SOURCE, LIBRARY_CLASSIFICATIONS_SOURCE, LIBRARY_PERSONALITIES_SOURCE,
  LIBRARY_PUBLISHERS_SOURCE, LIBRARY_COLLECTIONS_SOURCE, LIBRARY_DOCUMENT_CLEANUP_SOURCE,
  LIBRARY_CLASSIFICATION_SOURCE, LIBRARY_NORMALIZATION_SOURCE, NORMALIZATION_PAGE_IDS,
  GEMINI_PAGE_IDS, CLASSIFICATIONS_PAGE_IDS, PERSONALITIES_PAGE_IDS, PUBLISHERS_PAGE_IDS,
  COLLECTIONS_PAGE_IDS, DOCUMENT_CLEANUP_PAGE_IDS, createHarness,
} from "./support/page-harness.mjs";

test("tasks page bootstraps, renders global state, and wires SSE refresh", async () => {
  const payload = {
    event_cursor: 42,
    global: {
      active_tasks: 2,
      stop_all_state: "normal",
    },
    flows: [
      {
        panel_id: "maintenance",
        title: "Operations",
        tasks: [
          {
            task_id: "maintenance.quick",
            slug: "quick",
            title: "Quick",
            task_type: "scan",
            gemini_workers: {
              default: 1,
              next_run: 2,
              active: null,
              max: 4,
              editable: true,
            },
            run: {
              status: "running",
              started_at: "2026-03-24T10:00:00Z",
              finished_at: null,
              progress: { current: 3, total: 12, percent: 25 },
            },
          },
        ],
      },
    ],
  };
  const harness = createHarness({
    source: TASKS_PAGE_SOURCE,
    ids: ["global-status", "stop-all-btn", "task-flow-grid", "last-event"],
    apiResolver(path) {
      if (path === "/api/tasks") return JSON.parse(JSON.stringify(payload));
      if (path === "/api/tasks/maintenance.quick/toggle") return { action: "stop_graceful" };
      if (path === "/api/tasks/maintenance.quick/gemini-workers") return { workers: 3 };
      if (path === "/api/system/stop-all") return { action: "stop_all_graceful" };
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  assert.equal(Boolean(harness.sse.config), true);
  assert.equal(harness.sse.started, 1);
  assert.equal(harness.sse.config.initialCursor, 42);
  assert.equal(harness.elements.get("global-status").textContent, "Tasks: 2");
  assert.equal(harness.elements.get("stop-all-btn").dataset.stopState, "normal");
  assert.match(harness.elements.get("task-flow-grid").innerHTML, /\/tasks\/quick/);
  assert.match(harness.elements.get("task-flow-grid").innerHTML, /task-status-running is-active has-progress/);
  assert.match(harness.elements.get("task-flow-grid").innerHTML, /3 \/ 12/);
  assert.match(harness.elements.get("task-flow-grid").innerHTML, /25%/);
  assert.match(harness.elements.get("task-flow-grid").innerHTML, /data-task-toggle-id="maintenance.quick"/);
  assert.match(
    harness.elements.get("task-flow-grid").innerHTML,
    /<input[^>]+type="number"[^>]+min="1"[^>]+max="4"[^>]+value="2"/,
  );
  assert.doesNotMatch(harness.elements.get("task-flow-grid").innerHTML, />Workers</);
  assert.doesNotMatch(harness.elements.get("task-flow-grid").innerHTML, /<select|<option/);
  assert.doesNotMatch(harness.elements.get("task-flow-grid").innerHTML, />scan</);
  assert.doesNotMatch(harness.elements.get("task-flow-grid").innerHTML, /24\.03\.2026|2026-03-24/);

  const workerInput = {
    dataset: { geminiWorkersTask: "maintenance.quick" },
    disabled: false,
    value: "3",
    closest(selector) {
      return selector === "[data-gemini-workers-task]" ? this : null;
    },
  };
  harness.elements.get("task-flow-grid").dispatch("change", { target: workerInput });
  await harness.flush();
  assert.equal(
    harness.apiCalls.filter((call) => call.path === "/api/tasks/maintenance.quick/gemini-workers").length,
    1,
  );
  assert.deepEqual(
    JSON.parse(harness.apiCalls.find(
      (call) => call.path === "/api/tasks/maintenance.quick/gemini-workers",
    ).options.body),
    { workers: 3 },
  );

  harness.elements.get("task-flow-grid").dispatch("click", {
    target: { dataset: { taskToggleId: "maintenance.quick" }, disabled: false },
  });
  await harness.flush();
  assert.equal(
    harness.apiCalls.filter((call) => call.path === "/api/tasks/maintenance.quick/toggle").length,
    1,
  );

  const before = harness.apiCalls.filter((call) => call.path === "/api/tasks").length;
  harness.sse.config.onEvent({ type: "task.completed" }, { lastEventId: "18" });
  await harness.timer.runAllTimeouts();
  await harness.flush();

  const after = harness.apiCalls.filter((call) => call.path === "/api/tasks").length;
  assert.ok(after > before);
  assert.equal(harness.elements.get("last-event").textContent, "BANNER:task.completed");
});

test("tasks page renders empty state when no tasks exist", async () => {
  const payload = {
    global: {
      active_tasks: 0,
      stop_all_state: "disabled",
    },
    flows: [{ panel_id: "maintenance", title: "Operations", tasks: [] }],
  };
  const harness = createHarness({
    source: TASKS_PAGE_SOURCE,
    ids: ["global-status", "stop-all-btn", "task-flow-grid", "last-event"],
    apiResolver(path) {
      if (path === "/api/tasks") return JSON.parse(JSON.stringify(payload));
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(harness.elements.get("task-flow-grid").innerHTML, /No tasks available yet/);
});

test("tasks page pulses an unopened terminal run and records task navigation locally", async () => {
  const storageKey = "manzara.task-review.v1";
  const payload = {
    global: { active_tasks: 0, stop_all_state: "disabled" },
    flows: [
      {
        panel_id: "metadata",
        title: "Metadata",
        tasks: [
          {
            task_id: "library.metadata_extract",
            slug: "extract-metadata",
            title: "Extract metadata",
            task_type: "metadata",
            run: {
              run_id: 88,
              status: "completed",
              finished_at: "2026-08-30T06:00:00Z",
            },
          },
          {
            task_id: "library.metadata_validate",
            slug: "validate-metadata",
            title: "Validate metadata",
            task_type: "scan",
            run: {
              run_id: 89,
              status: "failed",
              finished_at: "2026-08-30T06:10:00Z",
            },
          },
        ],
      },
    ],
  };
  const harness = createHarness({
    source: TASKS_PAGE_SOURCE,
    ids: ["global-status", "stop-all-btn", "task-flow-grid", "last-event"],
    localStorageEntries: {
      [storageKey]: JSON.stringify({ version: 1, catalog_initialized: true, opened: {} }),
    },
    apiResolver(path) {
      if (path === "/api/tasks") return JSON.parse(JSON.stringify(payload));
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  const catalog = harness.elements.get("task-flow-grid");
  assert.match(catalog.innerHTML, /task-result-unread task-result-unread-completed/);
  assert.match(catalog.innerHTML, /task-result-unread task-result-unread-failed/);

  const removedClasses = [];
  const taskCard = {
    classList: {
      remove(...classes) {
        removedClasses.push(...classes);
      },
    },
  };
  const taskLink = {
    dataset: { taskOpenId: "library.metadata_extract", taskOpenRunId: "88" },
    closest(selector) {
      if (selector === "[data-task-open-id]") return this;
      if (selector === ".task-list-item") return taskCard;
      return null;
    },
  };
  catalog.dispatch("click", { target: taskLink });

  const stored = JSON.parse(harness.localStorage.getItem(storageKey));
  assert.equal(stored.opened["library.metadata_extract"], 88);
  assert.deepEqual(removedClasses, [
    "task-result-unread",
    "task-result-unread-completed",
    "task-result-unread-failed",
  ]);
});

test("tasks page renders error state when API fails", async () => {
  const harness = createHarness({
    source: TASKS_PAGE_SOURCE,
    ids: ["global-status", "stop-all-btn", "task-flow-grid", "last-event"],
    apiResolver(path) {
      if (path === "/api/tasks") {
        throw new Error("tasks unavailable");
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(harness.elements.get("task-flow-grid").innerHTML, /Error: tasks unavailable/);
});

test("tasks page stop-all does not call API when force-stop confirmation is rejected", async () => {
  const payload = {
    global: {
      active_tasks: 1,
      stop_all_state: "armed",
    },
    flows: [],
  };
  const harness = createHarness({
    source: TASKS_PAGE_SOURCE,
    ids: ["global-status", "stop-all-btn", "task-flow-grid", "last-event"],
    confirmResult: false,
    apiResolver(path) {
      if (path === "/api/tasks") return JSON.parse(JSON.stringify(payload));
      if (path === "/api/system/stop-all") return { action: "stop_all_force" };
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();

  harness.elements.get("stop-all-btn").dispatch("click");
  await harness.flush();
  await harness.timer.runAllTimeouts();
  await harness.flush();

  const stopCalls = harness.apiCalls.filter((call) => call.path === "/api/system/stop-all");
  assert.equal(stopCalls.length, 0);
});

test("task page renders running control state and toggles task endpoint", async () => {
  const detailPayload = {
    task: {
      task_id: "maintenance.quick",
      slug: "quick",
      title: "Quick",
      task_type: "scan",
      icon_idle: "Play",
    },
    panel: { title: "Operations" },
    stats: {
      total_runs: 1,
      status_counts: { completed: 0, failed: 0 },
      last_success_at: null,
    },
    runs: [
      {
        run_id: 11,
        status: "running",
        started_at: "2026-03-24T10:00:00Z",
        finished_at: null,
        exit_code: null,
        error_text: null,
        progress: {
          current: 3,
          total: 12,
          percent: 25,
        },
      },
    ],
    global: {
      active_tasks: 1,
      stop_all_state: "normal",
    },
  };

  const harness = createHarness({
    source: TASK_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "task-toggle-btn",
      "task-title",
      "task-subtitle",
      "task-stat-grid",
      "task-run-list",
      "run-result",
      "last-event",
      "copy-logs",
      "log-title",
      "log-content",
    ],
    locationPathname: "/tasks/quick",
    apiResolver(path) {
      if (path === "/api/tasks/quick?limit=20") {
        return JSON.parse(JSON.stringify(detailPayload));
      }
      if (path === "/api/tasks/maintenance.quick/toggle") {
        return { action: "stop_graceful" };
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  const toggleBtn = harness.elements.get("task-toggle-btn");
  assert.equal(toggleBtn.classList.contains("active"), true);
  assert.match(toggleBtn.innerHTML, /square/);
  assert.match(harness.elements.get("run-result").innerHTML, /task-run-progress/);
  assert.match(harness.elements.get("run-result").innerHTML, /3 \/ 12/);
  assert.match(harness.elements.get("run-result").innerHTML, /25%/);
  assert.match(harness.elements.get("run-result").innerHTML, /role="progressbar"/);

  toggleBtn.dispatch("click");
  await harness.flush();
  await harness.timer.runAllTimeouts();
  await harness.flush();

  const toggleCalls = harness.apiCalls.filter((call) => call.path === "/api/tasks/maintenance.quick/toggle");
  assert.equal(toggleCalls.length, 1);
  const detailCalls = harness.apiCalls.filter((call) => call.path === "/api/tasks/quick?limit=20");
  assert.ok(detailCalls.length >= 2);
});

test("task page opens logs inline and returns to overview", async () => {
  const detailPayload = {
    event_cursor: 12,
    task: {
      task_id: "maintenance.quick",
      slug: "quick",
      title: "Quick",
      task_type: "scan",
      icon_idle: "Play",
    },
    panel: { title: "Operations" },
    stats: {
      total_runs: 2,
      status_counts: { completed: 1, failed: 1 },
      last_success_at: "2026-03-24T10:00:01Z",
    },
    runs: [
      {
        run_id: 11,
        status: "completed",
        started_at: "2026-03-24T10:00:00Z",
        finished_at: "2026-03-24T10:00:01Z",
        exit_code: 0,
        error_text: null,
        summary: { message: "Done" },
      },
      {
        run_id: 10,
        status: "failed",
        started_at: "2026-03-23T10:00:00Z",
        finished_at: "2026-03-23T10:00:02Z",
        exit_code: 1,
        error_text: "Failed",
        summary: { message: "Failed" },
      },
    ],
    global: { active_tasks: 0, stop_all_state: "disabled" },
  };
  const harness = createHarness({
    source: TASK_SOURCE,
    ids: [
      "global-status", "stop-all-btn", "task-toggle-btn", "task-title", "task-subtitle",
      "task-stat-grid", "task-run-list", "run-result", "last-event", "workspace-tab-overview",
      "workspace-tab-logs", "run-overview-panel", "run-logs-panel", "log-title",
      "log-viewer-state", "log-surface", "log-empty-state", "copy-logs", "log-content",
    ],
    locationPathname: "/tasks/quick",
    apiResolver(path) {
      if (path === "/api/tasks/quick?limit=20") return JSON.parse(JSON.stringify(detailPayload));
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  const reviewState = JSON.parse(harness.localStorage.getItem("manzara.task-review.v1"));
  assert.equal(reviewState.opened["maintenance.quick"], 11);
  assert.equal(harness.elements.get("workspace-tab-overview").getAttribute("aria-selected"), "true");
  assert.equal(harness.elements.get("run-logs-panel").hidden, true);
  assert.deepEqual(harness.logViewer.openCalls, []);

  harness.elements.get("run-result").dispatch("click", {
    target: {
      closest(selector) {
        return selector === "#show-run-logs" ? { dataset: { runId: "11" } } : null;
      },
    },
  });
  await harness.flush();
  assert.equal(harness.elements.get("workspace-tab-logs").getAttribute("aria-selected"), "true");
  assert.equal(harness.elements.get("run-logs-panel").hidden, false);
  assert.match(harness.elements.get("log-title").textContent, /Quick.*run 11/i);
  assert.deepEqual(harness.logViewer.openCalls, [11]);

  harness.elements.get("task-run-list").dispatch("click", {
    target: {
      closest(selector) {
        return selector === ".task-run-row" ? { dataset: { runId: "10" } } : null;
      },
    },
  });
  await harness.flush();
  assert.deepEqual(harness.logViewer.openCalls, [11, 10]);
  assert.match(harness.elements.get("log-title").textContent, /run 10/i);

  harness.elements.get("workspace-tab-overview").dispatch("click");
  assert.equal(harness.elements.get("workspace-tab-overview").getAttribute("aria-selected"), "true");
  assert.equal(harness.elements.get("run-logs-panel").hidden, true);
  assert.equal(harness.logViewer.activeRunId, null);

  const tablist = harness.document.querySelector(".run-workspace-tabs");
  tablist.dispatch("keydown", {
    key: "ArrowRight",
    preventDefault() {},
    target: {
      closest(selector) {
        return selector === "[role='tab']" ? harness.elements.get("workspace-tab-overview") : null;
      },
    },
  });
  await harness.flush();
  assert.equal(harness.elements.get("workspace-tab-logs").getAttribute("aria-selected"), "true");
});

test("tasks catalog presents conveyor steps in left-to-right execution order", async () => {
  let revision = 0;
  let stages = [];
  const catalogPayload = {
    event_cursor: 9,
    global: { active_tasks: 0, stop_all_state: "disabled" },
    flows: [
      {
        panel_id: "maintenance",
        title: "Operations",
        tasks: [
          {
            task_id: "maintenance.quick",
            slug: "quick",
            title: "Quick",
            task_type: "scan",
            run: { status: "idle" },
          },
        ],
      },
    ],
    conveyor: {
      definition: { revision, stages },
      run: null,
      items: [],
      available_tasks: [
        {
          task_id: "maintenance.quick",
          panel_id: "maintenance",
          panel_title: "Operations",
          title: "Quick",
          task_type: "scan",
          icon_idle: "Play",
        },
      ],
    },
  };
  const ids = [
    "global-status", "stop-all-btn", "task-flow-grid", "last-event", "conveyor-status",
    "conveyor-stages", "conveyor-clear",
    "conveyor-run", "conveyor-stop",
  ];
  const harness = createHarness({
    source: TASKS_PAGE_SOURCE,
    ids,
    apiResolver(path, options) {
      if (path === "/api/tasks") {
        return JSON.parse(JSON.stringify({
          ...catalogPayload,
          conveyor: {
            ...catalogPayload.conveyor,
            definition: { revision, stages },
          },
        }));
      }
      if (path === "/api/conveyor" && options.method === "PUT") {
        const body = JSON.parse(options.body);
        assert.equal(body.revision, revision);
        stages = body.stages;
        revision += 1;
        return { definition: { revision, stages } };
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  assert.match(harness.elements.get("task-flow-grid").innerHTML, /data-conveyor-task-id="maintenance.quick"/);
  assert.match(harness.elements.get("conveyor-status").textContent, /left to right/i);
  assert.match(harness.elements.get("conveyor-stages").innerHTML, /aria-label="Drop first task here"/);
  assert.match(harness.elements.get("conveyor-stages").innerHTML, /data-lucide="plus"/);
  assert.doesNotMatch(harness.elements.get("conveyor-stages").innerHTML, />Drag a task badge here</);
  assert.equal(harness.elements.get("conveyor-stages").classList.contains("is-empty"), true);

  const transfer = { effectAllowed: "", setData() {} };
  harness.elements.get("task-flow-grid").dispatch("dragstart", {
    dataTransfer: transfer,
    target: {
      closest(selector) {
        return selector === "[data-conveyor-task-id]"
          ? { dataset: { conveyorTaskId: "maintenance.quick" } }
          : null;
      },
    },
  });
  harness.elements.get("conveyor-stages").dispatch("drop", {
    preventDefault() {},
    target: {
      closest(selector) {
        return selector === "[data-new-stage-index]"
          ? { dataset: { newStageIndex: "0" } }
          : null;
      },
    },
  });
  await harness.flush();

  const saves = harness.apiCalls.filter((call) => call.path === "/api/conveyor");
  assert.equal(saves.length, 1);
  assert.equal(stages.length, 1);
  assert.equal(stages[0].items[0].task_id, "maintenance.quick");
  assert.match(harness.elements.get("conveyor-stages").innerHTML, /Step 1/);
  assert.match(harness.elements.get("conveyor-stages").innerHTML, /class="conveyor-stage-slot"/);
  assert.match(harness.elements.get("conveyor-stages").innerHTML, /aria-label="Add step 2"/);
  assert.equal(harness.elements.get("conveyor-stages").classList.contains("is-empty"), false);
});

test("task page normalizes idle icon names for lucide glyph rendering", async () => {
  const detailPayload = {
    task: {
      task_id: "maintenance.scan_test",
      slug: "scan",
      title: "Scan for changes",
      task_type: "scan",
      icon_idle: "RefreshCw",
    },
    panel: { title: "Operations" },
    stats: {
      total_runs: 1,
      status_counts: { completed: 1, failed: 0 },
      last_success_at: "2026-03-24T10:00:01Z",
    },
    runs: [
      {
        run_id: 51,
        status: "completed",
        started_at: "2026-03-24T10:00:00Z",
        finished_at: "2026-03-24T10:00:01Z",
        exit_code: 0,
        error_text: null,
        summary: { status: "completed", message: "Done" },
      },
    ],
    global: {
      active_tasks: 0,
      stop_all_state: "disabled",
    },
  };

  const harness = createHarness({
    source: TASK_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "task-toggle-btn",
      "task-title",
      "task-subtitle",
      "task-stat-grid",
      "task-run-list",
      "run-result",
      "last-event",
      "copy-logs",
      "log-title",
      "log-content",
    ],
    locationPathname: "/tasks/scan",
    apiResolver(path) {
      if (path === "/api/tasks/scan?limit=20") {
        return JSON.parse(JSON.stringify(detailPayload));
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  const toggleBtn = harness.elements.get("task-toggle-btn");
  assert.match(toggleBtn.innerHTML, /data-lucide="refresh-cw"/);
});

test("task page renders structured run artifacts from backend summary", async () => {
  const detailPayload = {
    task: {
      task_id: "maintenance.scan_test",
      slug: "scan",
      title: "Scan for changes",
      task_type: "scan",
      icon_idle: "RefreshCw",
    },
    panel: { title: "Operations" },
    stats: {
      total_runs: 1,
      status_counts: { completed: 1, failed: 0 },
      last_success_at: "2026-03-24T10:00:01Z",
    },
    runs: [
      {
        run_id: 51,
        status: "completed",
        started_at: "2026-03-24T10:00:00Z",
        finished_at: "2026-03-24T10:00:01Z",
        exit_code: 0,
        error_text: null,
        summary: {
          status: "completed",
          message: "Scan completed",
          artifacts: {
            kind: "library.test_artifact",
            episodes_added: 3,
            episodes_changed: 2,
            episodes_removed: 1,
          },
        },
      },
    ],
    global: {
      active_tasks: 0,
      stop_all_state: "disabled",
    },
  };

  const harness = createHarness({
    source: TASK_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "task-toggle-btn",
      "task-title",
      "task-subtitle",
      "task-stat-grid",
      "task-run-list",
      "run-result",
      "last-event",
      "copy-logs",
      "log-title",
      "log-content",
    ],
    locationPathname: "/tasks/scan",
    apiResolver(path) {
      if (path === "/api/tasks/scan?limit=20") {
        return JSON.parse(JSON.stringify(detailPayload));
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  await harness.flush();
  const html = harness.elements.get("run-result").innerHTML;
  assert.match(html, /Run artifacts/i);
  assert.match(html, /episodes_added/i);
  assert.match(html, /test_artifact/i);
  assert.doesNotMatch(html, /Detailed changes/i);
});


test("task page applies toggle response run and enables logs immediately", async () => {
  const detailPayload = {
    task: {
      task_id: "maintenance.scan_test",
      slug: "scan",
      title: "Scan for changes",
      task_type: "scan",
      icon_idle: "RefreshCw",
    },
    panel: { title: "Operations" },
    stats: {
      total_runs: 0,
      status_counts: { completed: 0, failed: 0 },
      last_success_at: null,
    },
    runs: [],
    global: {
      active_tasks: 0,
      stop_all_state: "disabled",
    },
  };

  const harness = createHarness({
    source: TASK_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "task-toggle-btn",
      "task-title",
      "task-subtitle",
      "task-stat-grid",
      "task-run-list",
      "run-result",
      "last-event",
      "copy-logs",
      "log-title",
      "log-content",
    ],
    locationPathname: "/tasks/scan",
    apiResolver(path) {
      if (path === "/api/tasks/scan?limit=20") {
        return JSON.parse(JSON.stringify(detailPayload));
      }
      if (path === "/api/tasks/maintenance.scan_test/toggle") {
        return {
          action: "start",
          run: {
            run_id: 88,
            status: "starting",
            started_at: "2026-03-24T10:00:05Z",
            finished_at: null,
            exit_code: null,
            error_text: null,
          },
        };
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  assert.match(harness.elements.get("run-result").innerHTML, /No run selected|No runs yet/);
  harness.elements.get("task-toggle-btn").dispatch("click");
  await harness.flush();
  assert.match(harness.elements.get("run-result").innerHTML, /#88|Run starting/i);
  assert.match(harness.elements.get("run-result").innerHTML, /show-run-logs/i);

  const toggleCall = harness.apiCalls.find((call) => call.path.endsWith("/toggle"));
  assert.equal(toggleCall?.path, "/api/tasks/maintenance.scan_test/toggle");
});

test("task page renders loading then error when task detail fetch fails", async () => {
  const harness = createHarness({
    source: TASK_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "task-toggle-btn",
      "task-title",
      "task-subtitle",
      "task-stat-grid",
      "task-run-list",
      "run-result",
      "last-event",
      "copy-logs",
      "log-title",
      "log-content",
    ],
    locationPathname: "/tasks/quick",
    apiResolver(path) {
      if (path === "/api/tasks/quick?limit=20") {
        throw new Error("detail unavailable");
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.equal(harness.elements.get("task-title").textContent, "Task unavailable");
  assert.match(harness.elements.get("task-run-list").innerHTML, /Error: detail unavailable/);
});
