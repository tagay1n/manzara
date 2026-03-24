import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const TASKS_SOURCE = readFileSync(new URL("../../static/tasks.js", import.meta.url), "utf-8");
const TASK_SOURCE = readFileSync(new URL("../../static/task.js", import.meta.url), "utf-8");
const DASHBOARD_SOURCE = readFileSync(new URL("../../static/app.js", import.meta.url), "utf-8");
const SCHEDULES_SOURCE = readFileSync(new URL("../../static/schedules.js", import.meta.url), "utf-8");
const LIBRARY_SOURCE = readFileSync(new URL("../../static/library.js", import.meta.url), "utf-8");
const DATABASE_SOURCE = readFileSync(new URL("../../static/database.js", import.meta.url), "utf-8");

class FakeClassList {
  constructor() {
    this._set = new Set();
  }
  add(...names) {
    names.forEach((name) => this._set.add(String(name)));
  }
  remove(...names) {
    names.forEach((name) => this._set.delete(String(name)));
  }
  contains(name) {
    return this._set.has(String(name));
  }
}

class FakeElement {
  constructor(id) {
    this.id = id;
    this.textContent = "";
    this.innerHTML = "";
    this.disabled = false;
    this.title = "";
    this.classList = new FakeClassList();
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.open = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
  }
  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }
  getAttribute(name) {
    return this.attributes.get(String(name)) || null;
  }
  addEventListener(type, handler) {
    const key = String(type);
    const list = this.listeners.get(key) || [];
    list.push(handler);
    this.listeners.set(key, list);
  }
  dispatch(type, event = {}) {
    const list = this.listeners.get(String(type)) || [];
    for (const handler of list) {
      handler({
        target: this,
        currentTarget: this,
        ...event,
      });
    }
  }
  closest() {
    return null;
  }
  showModal() {
    this.open = true;
  }
  close() {
    this.open = false;
  }
}

function createTimerHarness() {
  let nextId = 1;
  const timeouts = new Map();
  return {
    setTimeout(fn, _ms) {
      const id = nextId++;
      timeouts.set(id, fn);
      return id;
    },
    clearTimeout(id) {
      timeouts.delete(id);
    },
    async runAllTimeouts() {
      while (timeouts.size > 0) {
        const jobs = [...timeouts.values()];
        timeouts.clear();
        for (const fn of jobs) {
          await fn();
        }
        await Promise.resolve();
      }
    },
  };
}

function createHarness({
  source,
  ids,
  apiResolver,
  confirmResult = true,
  locationPathname = "/tasks",
}) {
  const elements = new Map();
  for (const id of ids) {
    elements.set(id, new FakeElement(id));
  }

  const timer = createTimerHarness();
  const apiCalls = [];
  const sse = {
    config: null,
    started: 0,
    stopped: 0,
  };

  const windowObj = {
    location: { pathname: locationPathname },
    history: {
      replaceState(_state, _title, nextPath) {
        windowObj.location.pathname = String(nextPath);
      },
    },
    addEventListener() {},
    confirm() {
      return confirmResult;
    },
    alert() {},
    ManzaraSound: null,
    ManzaraCore: {
      DEFAULT_EVENT_TYPES: ["task.started", "task.completed", "task.failed"],
      async api(path, options = {}) {
        apiCalls.push({ path: String(path), options: { ...options } });
        return apiResolver(String(path), options);
      },
      escapeHtml(value) {
        return String(value)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;");
      },
      cssName(name, fallback = "unknown") {
        const value = String(name || "").trim().toLowerCase();
        if (!value) return fallback;
        return value.replace(/[^a-z0-9_-]+/g, "-");
      },
      isActiveStatus(status) {
        const value = String(status || "");
        return (
          value === "starting" ||
          value === "running" ||
          value === "stopping_graceful" ||
          value === "stopping_force"
        );
      },
      formatDateTime(value) {
        return `DT:${String(value)}`;
      },
      formatGlobalStatus(activeTasks, activeWorkflows) {
        return `Tasks: ${Number(activeTasks || 0)} • Flows: ${Number(activeWorkflows || 0)}`;
      },
      formatEventBanner(payload) {
        return `BANNER:${String(payload?.type || "")}`;
      },
      applyStopAllButton(button, stopAllState) {
        button.dataset.stopState = String(stopAllState || "");
      },
      createSseController(options = {}) {
        sse.config = options;
        return {
          start() {
            sse.started += 1;
          },
          stop() {
            sse.stopped += 1;
          },
        };
      },
    },
  };

  const documentObj = {
    getElementById(id) {
      const key = String(id);
      if (!elements.has(key)) {
        elements.set(key, new FakeElement(key));
      }
      return elements.get(key);
    },
  };

  const sandbox = {
    window: windowObj,
    document: documentObj,
    console,
    alert: (...args) => windowObj.alert(...args),
    confirm: (...args) => windowObj.confirm(...args),
    lucide: { createIcons() {} },
    navigator: {
      clipboard: {
        async writeText(_text) {},
      },
    },
    setTimeout: timer.setTimeout.bind(timer),
    clearTimeout: timer.clearTimeout.bind(timer),
    setInterval: () => 1,
    clearInterval: () => {},
    encodeURIComponent,
    decodeURIComponent,
    Date,
    Number,
    Promise,
  };

  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);

  return {
    window: windowObj,
    document: documentObj,
    elements,
    timer,
    apiCalls,
    sse,
    async flush() {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    },
  };
}

test("tasks page bootstraps, renders global state, and wires SSE refresh", async () => {
  const payload = {
    global: {
      active_tasks: 2,
      active_workflows: 1,
      stop_all_state: "normal",
    },
    flows: [
      {
        panel_id: "shayan",
        title: "Shayan",
        tasks: [
          {
            task_id: "shayan.quick",
            slug: "quick",
            title: "Quick",
            task_type: "scan",
            run: {
              status: "running",
              started_at: "2026-03-24T10:00:00Z",
              finished_at: null,
            },
          },
        ],
      },
    ],
  };
  const harness = createHarness({
    source: TASKS_SOURCE,
    ids: ["global-status", "stop-all-btn", "task-flow-grid", "last-event"],
    apiResolver(path) {
      if (path === "/api/tasks") return JSON.parse(JSON.stringify(payload));
      if (path === "/api/system/stop-all") return { action: "stop_all_graceful" };
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  assert.equal(Boolean(harness.sse.config), true);
  assert.equal(harness.sse.started, 1);
  assert.equal(harness.elements.get("global-status").textContent, "Tasks: 2 • Flows: 1");
  assert.equal(harness.elements.get("stop-all-btn").dataset.stopState, "normal");
  assert.match(harness.elements.get("task-flow-grid").innerHTML, /\/tasks\/quick/);

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
      active_workflows: 0,
      stop_all_state: "disabled",
    },
    flows: [{ panel_id: "shayan", title: "Shayan", tasks: [] }],
  };
  const harness = createHarness({
    source: TASKS_SOURCE,
    ids: ["global-status", "stop-all-btn", "task-flow-grid", "last-event"],
    apiResolver(path) {
      if (path === "/api/tasks") return JSON.parse(JSON.stringify(payload));
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(harness.elements.get("task-flow-grid").innerHTML, /No tasks available yet/);
});

test("tasks page renders error state when API fails", async () => {
  const harness = createHarness({
    source: TASKS_SOURCE,
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
      active_workflows: 0,
      stop_all_state: "armed",
    },
    flows: [],
  };
  const harness = createHarness({
    source: TASKS_SOURCE,
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
      task_id: "shayan.quick",
      slug: "quick",
      title: "Quick",
      task_type: "scan",
      icon_idle: "Play",
    },
    panel: { title: "Shayan" },
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
      },
    ],
    global: {
      active_tasks: 1,
      active_workflows: 0,
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
      "close-logs",
      "log-dialog",
      "copy-logs",
      "log-title",
      "log-content",
    ],
    locationPathname: "/tasks/quick",
    apiResolver(path) {
      if (path === "/api/tasks/quick?limit=120") {
        return JSON.parse(JSON.stringify(detailPayload));
      }
      if (path === "/api/tasks/quick/toggle") {
        return { action: "stop_graceful" };
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  const toggleBtn = harness.elements.get("task-toggle-btn");
  assert.equal(toggleBtn.classList.contains("active"), true);
  assert.match(toggleBtn.innerHTML, /square/);

  toggleBtn.dispatch("click");
  await harness.flush();
  await harness.timer.runAllTimeouts();
  await harness.flush();

  const toggleCalls = harness.apiCalls.filter((call) => call.path === "/api/tasks/quick/toggle");
  assert.equal(toggleCalls.length, 1);
  const detailCalls = harness.apiCalls.filter((call) => call.path === "/api/tasks/quick?limit=120");
  assert.ok(detailCalls.length >= 2);
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
      "close-logs",
      "log-dialog",
      "copy-logs",
      "log-title",
      "log-content",
    ],
    locationPathname: "/tasks/quick",
    apiResolver(path) {
      if (path === "/api/tasks/quick?limit=120") {
        throw new Error("detail unavailable");
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.equal(harness.elements.get("task-title").textContent, "Task unavailable");
  assert.match(harness.elements.get("task-run-list").innerHTML, /Error: detail unavailable/);
});

test("dashboard page renders empty state for panels and runs", async () => {
  const payload = {
    global: {
      active_tasks: 0,
      active_workflows: 0,
      stop_all_state: "disabled",
    },
    panels: [],
    recent_runs: [],
  };
  const harness = createHarness({
    source: DASHBOARD_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "panel-grid",
      "runs-list",
      "last-event",
      "close-logs",
      "log-dialog",
      "copy-logs",
      "log-title",
      "log-content",
    ],
    apiResolver(path) {
      if (path === "/api/dashboard") return JSON.parse(JSON.stringify(payload));
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(harness.elements.get("panel-grid").innerHTML, /No flows available yet/);
  assert.match(harness.elements.get("runs-list").innerHTML, /No runs yet/);
  assert.equal(harness.elements.get("global-status").textContent, "Tasks: 0 • Flows: 0");
});

test("dashboard page renders error state when API fails", async () => {
  const harness = createHarness({
    source: DASHBOARD_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "panel-grid",
      "runs-list",
      "last-event",
      "close-logs",
      "log-dialog",
      "copy-logs",
      "log-title",
      "log-content",
    ],
    apiResolver(path) {
      if (path === "/api/dashboard") {
        throw new Error("dashboard unavailable");
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(harness.elements.get("panel-grid").innerHTML, /Error: dashboard unavailable/);
  assert.match(harness.elements.get("runs-list").innerHTML, /Error: dashboard unavailable/);
});

test("schedules page renders empty state when no workflows exist", async () => {
  const payload = {
    global: {
      active_tasks: 0,
      active_workflows: 0,
      stop_all_state: "disabled",
    },
    workflows: [],
  };
  const harness = createHarness({
    source: SCHEDULES_SOURCE,
    ids: ["global-status", "stop-all-btn", "schedule-grid", "last-event"],
    apiResolver(path) {
      if (path === "/api/schedules") return JSON.parse(JSON.stringify(payload));
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(harness.elements.get("schedule-grid").innerHTML, /No schedules available yet/);
  assert.equal(harness.elements.get("global-status").textContent, "Tasks: 0 • Flows: 0");
});

test("schedules page renders error state when API fails", async () => {
  const harness = createHarness({
    source: SCHEDULES_SOURCE,
    ids: ["global-status", "stop-all-btn", "schedule-grid", "last-event"],
    apiResolver(path) {
      if (path === "/api/schedules") {
        throw new Error("schedules unavailable");
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(harness.elements.get("schedule-grid").innerHTML, /Error: schedules unavailable/);
});

test("library page renders loading then API error state", async () => {
  const harness = createHarness({
    source: LIBRARY_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "last-event",
      "library-status",
      "library-stat-grid",
      "library-top-list",
      "library-last-run",
    ],
    apiResolver(path) {
      if (path === "/api/library") {
        throw new Error("library unavailable");
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(harness.elements.get("library-status").textContent, /Library unavailable/);
  assert.match(harness.elements.get("library-stat-grid").innerHTML, /Error: library unavailable/);
  assert.match(harness.elements.get("library-top-list").innerHTML, /Error: library unavailable/);
});

test("database page renders loading then API error state", async () => {
  const harness = createHarness({
    source: DATABASE_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "last-event",
      "db-warning-pill",
      "db-status",
      "db-stat-grid",
      "db-backup-grid",
      "db-table-body",
      "db-table-footnote",
    ],
    apiResolver(path) {
      if (path === "/api/database/state") {
        throw new Error("database unavailable");
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.equal(harness.elements.get("db-warning-pill").textContent, "Unavailable");
  assert.match(harness.elements.get("db-status").textContent, /Database state unavailable/);
  assert.match(harness.elements.get("db-stat-grid").innerHTML, /Error: database unavailable/);
  assert.match(harness.elements.get("db-table-body").innerHTML, /Error: database unavailable/);
});
