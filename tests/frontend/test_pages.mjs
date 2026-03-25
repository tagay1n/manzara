import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const TASKS_SOURCE = readFileSync(new URL("../../static/tasks.js", import.meta.url), "utf-8");
const TASK_SOURCE = readFileSync(new URL("../../static/task.js", import.meta.url), "utf-8");
const FLOW_SOURCE = readFileSync(new URL("../../static/flow.js", import.meta.url), "utf-8");
const DASHBOARD_SOURCE = readFileSync(new URL("../../static/app.js", import.meta.url), "utf-8");
const SCHEDULES_SOURCE = readFileSync(new URL("../../static/schedules.js", import.meta.url), "utf-8");
const LIBRARY_SOURCE = readFileSync(new URL("../../static/library.js", import.meta.url), "utf-8");
const DATABASE_SOURCE = readFileSync(new URL("../../static/database.js", import.meta.url), "utf-8");
const LIBRARY_CLASSIFICATIONS_SOURCE = readFileSync(
  new URL("../../static/library-classifications.js", import.meta.url),
  "utf-8",
);
const LIBRARY_PERSONALITIES_SOURCE = readFileSync(
  new URL("../../static/library-personalities.js", import.meta.url),
  "utf-8",
);
const LIBRARY_PUBLISHERS_SOURCE = readFileSync(
  new URL("../../static/library-publishers.js", import.meta.url),
  "utf-8",
);
const LIBRARY_COLLECTIONS_SOURCE = readFileSync(
  new URL("../../static/library-collections.js", import.meta.url),
  "utf-8",
);
const LIBRARY_CLASSIFICATION_SOURCE = readFileSync(
  new URL("../../static/library-classification.js", import.meta.url),
  "utf-8",
);
const LIBRARY_NORMALIZATION_SOURCE = readFileSync(
  new URL("../../static/library-normalization.js", import.meta.url),
  "utf-8",
);

const NORMALIZATION_PAGE_IDS = [
  "global-status",
  "stop-all-btn",
  "last-event",
  "normalization-title",
  "entity-source-link",
  "normalization-status",
  "normalization-stat-grid",
  "canonical-status",
  "canonical-table-body",
  "tab-badge-canonicals",
  "queue-bulk-canonical",
  "queue-filter-search",
  "queue-filter-status",
  "queue-filter-script",
  "queue-filter-min-docs",
  "queue-status",
  "queue-table-body",
  "queue-page-label",
  "queue-page-prev",
  "queue-page-next",
  "suggestions-status",
  "tab-badge-suggestions",
  "suggestions-table-body",
  "merge-min-score",
  "merge-status",
  "merge-root",
  "quality-status",
  "quality-stat-grid",
  "quality-unresolved",
  "history-status",
  "history-root",
  "queue-filter-apply",
  "queue-select-all",
  "queue-bulk-link",
  "queue-bulk-reject",
  "queue-clear-selection",
  "canonical-create-btn",
  "canonical-create-name",
  "canonical-create-notes",
  "canonical-search-apply",
  "canonical-search",
  "suggestions-refresh-btn",
  "suggestions-limit",
  "suggestions-use-gemini",
  "merge-load-btn",
  "evidence-dialog",
  "evidence-title",
  "evidence-content",
  "evidence-close-btn",
  "evidence-close-footer-btn",
  "tab-btn-queue",
  "tab-btn-canonicals",
  "tab-btn-suggestions",
  "tab-btn-merge",
  "tab-btn-quality",
  "tab-btn-history",
  "tab-panel-queue",
  "tab-panel-canonicals",
  "tab-panel-suggestions",
  "tab-panel-merge",
  "tab-panel-quality",
  "tab-panel-history",
];

const CLASSIFICATIONS_PAGE_IDS = [
  "global-status",
  "stop-all-btn",
  "last-event",
  "classification-table-body",
  "classification-table-status",
  "distribution-root",
  "duplicates-root",
  "filter-apply",
  "filter-ddc-prefix",
  "filter-min-usage",
  "filter-search",
  "filter-sort",
  "filter-status",
  "merge-limit",
  "merge-min-score",
  "merge-refresh",
  "merge-root",
  "merge-status",
  "merge-summary",
  "normalization-affected",
  "normalization-drop-segments",
  "normalization-groups",
  "normalization-limit",
  "normalization-refresh",
  "normalization-status",
  "normalization-summary",
  "page-label",
  "page-next",
  "page-prev",
  "tab-badge-duplicates",
  "tab-badge-merge",
  "tab-badge-normalization",
  "tab-badge-unclassified",
  "tree-root",
  "unclassified-root",
];

const PERSONALITIES_PAGE_IDS = [
  "clusters-root",
  "filter-apply",
  "filter-min-docs",
  "filter-script",
  "filter-search",
  "filter-sort",
  "global-status",
  "last-event",
  "page-label",
  "page-next",
  "page-prev",
  "personality-stat-grid",
  "personality-status",
  "personality-table-body",
  "personality-table-status",
  "personality-top-list",
  "queue-root",
  "scripts-root",
  "stop-all-btn",
  "tab-badge-clusters",
  "tab-badge-queue",
  "tab-badge-scripts",
];

const PUBLISHERS_PAGE_IDS = [
  "clusters-root",
  "filter-apply",
  "filter-min-docs",
  "filter-script",
  "filter-search",
  "filter-sort",
  "global-status",
  "last-event",
  "page-label",
  "page-next",
  "page-prev",
  "publisher-stat-grid",
  "publisher-status",
  "publisher-table-body",
  "publisher-table-status",
  "publisher-top-list",
  "queue-root",
  "scripts-root",
  "stop-all-btn",
  "tab-badge-clusters",
  "tab-badge-queue",
  "tab-badge-scripts",
];

const COLLECTIONS_PAGE_IDS = [
  "global-status",
  "stop-all-btn",
  "last-event",
  "collections-status",
  "collections-stat-grid",
  "collections-top-list",
  "collections-table-status",
  "collections-table-body",
  "clusters-root",
  "queue-root",
  "tab-badge-clusters",
  "tab-badge-queue",
  "page-label",
  "page-prev",
  "page-next",
  "filter-search",
  "filter-status",
  "filter-include",
  "filter-sort",
  "filter-apply",
  "collection-items-status",
  "collection-items-body",
  "collection-title-input",
  "collection-notes-input",
  "collection-approve-btn",
  "collection-reject-btn",
  "collection-include-btn",
];

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
  toggle(name, force) {
    const key = String(name);
    if (force === true) {
      this._set.add(key);
      return true;
    }
    if (force === false) {
      this._set.delete(key);
      return false;
    }
    if (this._set.has(key)) {
      this._set.delete(key);
      return false;
    }
    this._set.add(key);
    return true;
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
    this.value = "";
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
  selectors = [],
  apiResolver,
  confirmResult = true,
  promptResult = null,
  locationPathname = "/tasks",
}) {
  const elements = new Map();
  for (const id of ids) {
    elements.set(id, new FakeElement(id));
  }
  for (const selector of selectors) {
    elements.set(`selector:${selector}`, new FakeElement(`selector:${selector}`));
    elements.set(`selectorAll:${selector}`, new FakeElement(`selectorAll:${selector}`));
  }

  const timer = createTimerHarness();
  const apiCalls = [];
  const alerts = [];
  const prompts = [];
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
    alert(message) {
      alerts.push(String(message || ""));
    },
    prompt(message, defaultValue = "") {
      prompts.push({
        message: String(message || ""),
        defaultValue: String(defaultValue || ""),
      });
      if (typeof promptResult === "function") {
        return promptResult({ message, defaultValue });
      }
      return promptResult;
    },
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
      setStatusMessage(node, text, options = {}) {
        if (!node) return;
        const isError = Boolean(options.error);
        node.textContent = String(text ?? "");
        node.classList.toggle("library-status-error", isError);
      },
      renderRunRowMessage(text, options = {}) {
        const isError = Boolean(options.error);
        const prefix = isError ? "Error: " : "";
        return `<div class="run-row">${prefix}${this.escapeHtml(String(text ?? ""))}</div>`;
      },
      renderWorkflowFootnoteMessage(text, options = {}) {
        const isError = Boolean(options.error);
        const classes = isError ? "workflow-footnote library-status-error" : "workflow-footnote";
        return `<div class="${classes}">${this.escapeHtml(String(text ?? ""))}</div>`;
      },
      renderLoadingTableRow(colSpan, text) {
        const safeColSpan = Math.max(1, Math.trunc(Number(colSpan) || 1));
        return `<tr><td colspan="${safeColSpan}">${this.escapeHtml(String(text ?? ""))}</td></tr>`;
      },
      applyPaginationControls(options = {}) {
        const page = Math.max(1, Math.trunc(Number(options.page) || 1));
        const totalPages = Math.max(1, Math.trunc(Number(options.totalPages) || 1));
        if (options.labelNode) {
          options.labelNode.textContent = `Page ${page} / ${totalPages}`;
        }
        if (options.prevNode) {
          options.prevNode.disabled = page <= 1;
        }
        if (options.nextNode) {
          options.nextNode.disabled = page >= totalPages;
        }
      },
      attachViewState(state, initial = "loading") {
        const allowed = new Set(["loading", "ready", "empty", "error"]);
        const target = state && typeof state === "object" ? state : {};
        const normalize = (value, fallback = "loading") => {
          const next = String(value || "").trim().toLowerCase();
          return allowed.has(next) ? next : fallback;
        };
        target.viewState = normalize(initial);
        return {
          get() {
            return normalize(target.viewState);
          },
          set(next) {
            const value = normalize(next, this.get());
            target.viewState = value;
            return value;
          },
          is(next) {
            return this.get() === normalize(next);
          },
        };
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
      createTabController(options = {}) {
        const tabs = Array.isArray(options.tabs)
          ? options.tabs.map((tab) => String(tab || "")).filter(Boolean)
          : [];
        const validTabs = new Set(tabs);
        const getActiveTab = typeof options.getActiveTab === "function"
          ? options.getActiveTab
          : () => "";
        const setActiveTab = typeof options.setActiveTab === "function"
          ? options.setActiveTab
          : () => {};
        return {
          tabs: [...tabs],
          isValid(tab) {
            return validTabs.has(String(tab || ""));
          },
          apply() {
            for (const tab of tabs) {
              const isActive = String(getActiveTab() || "") === tab;
              const btn = documentObj.getElementById(`tab-btn-${tab}`);
              const panel = documentObj.getElementById(`tab-panel-${tab}`);
              if (btn) {
                btn.classList.toggle("active", isActive);
                btn.setAttribute("aria-selected", isActive ? "true" : "false");
              }
              if (panel) {
                panel.classList.toggle("active", isActive);
              }
            }
          },
          select(tab) {
            const value = String(tab || "");
            if (!validTabs.has(value)) return false;
            setActiveTab(value);
            this.apply();
            return true;
          },
        };
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
      createRunLogViewer(options = {}) {
        let activeRunId = null;
        return {
          async open(runId, taskTitle = "Task") {
            activeRunId = Number(runId || 0);
            if (options.titleNode) {
              options.titleNode.textContent = `Logs • ${taskTitle} • run ${activeRunId}`;
            }
            if (options.contentNode) {
              options.contentNode.textContent = "";
            }
            if (options.dialogNode && !options.dialogNode.open) {
              options.dialogNode.showModal?.();
            }
          },
          close(closeOptions = {}) {
            activeRunId = null;
            if (options.contentNode && closeOptions.keepContent !== true) {
              options.contentNode.textContent = "";
            }
            if (closeOptions.closeDialog !== false && options.dialogNode?.open) {
              options.dialogNode.close?.();
            }
          },
          destroy() {
            this.close();
          },
          async loadOlder() {},
          async pollFollow() {},
          getState() {
            return {
              activeRunId,
              nextAfterLogId: 0,
              nextBeforeLogId: 0,
              hasMoreBefore: false,
              bufferedLines: 0,
            };
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
    querySelector(selector) {
      const key = `selector:${String(selector)}`;
      if (!elements.has(key)) {
        elements.set(key, new FakeElement(key));
      }
      return elements.get(key);
    },
    querySelectorAll(selector) {
      const key = `selectorAll:${String(selector)}`;
      if (!elements.has(key)) {
        elements.set(key, new FakeElement(key));
      }
      return [elements.get(key)];
    },
  };

  const sandbox = {
    window: windowObj,
    document: documentObj,
    console,
    alert: (...args) => windowObj.alert(...args),
    confirm: (...args) => windowObj.confirm(...args),
    prompt: (...args) => windowObj.prompt(...args),
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
    URLSearchParams,
    HTMLInputElement: FakeElement,
  };

  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);

  return {
    window: windowObj,
    document: documentObj,
    elements,
    timer,
    apiCalls,
    alerts,
    prompts,
    sse,
    async flush() {
      for (let i = 0; i < 16; i += 1) {
        await Promise.resolve();
      }
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
      if (path === "/api/tasks/quick?limit=20") {
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
  const detailCalls = harness.apiCalls.filter((call) => call.path === "/api/tasks/quick?limit=20");
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

test("flow page bootstraps, renders tasks and summaries, and refreshes on SSE", async () => {
  const payload = {
    global: {
      active_tasks: 1,
      active_workflows: 0,
      stop_all_state: "normal",
    },
    flow: {
      panel_id: "shayan",
      slug: "shayan",
      title: "Shayan",
      description: "Flow summary",
      stats_cards: [{ label: "Total Runs", value: "5" }],
    },
    tasks: [
      {
        task_id: "shayan.quick",
        slug: "quick",
        title: "Quick",
        task_type: "scan",
        icon_idle: "Play",
        icon_running: "Square",
        run: {
          run_id: 41,
          status: "completed",
          started_at: "2026-03-24T10:00:00Z",
          finished_at: "2026-03-24T10:00:01Z",
          exit_code: 0,
          error_text: null,
          summary: { status: "completed", message: "Quick run completed." },
        },
        runs: [
          {
            run_id: 41,
            status: "completed",
            started_at: "2026-03-24T10:00:00Z",
            finished_at: "2026-03-24T10:00:01Z",
            exit_code: 0,
            error_text: null,
            summary: { status: "completed", message: "Quick run completed." },
          },
        ],
      },
    ],
  };

  const harness = createHarness({
    source: FLOW_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "flow-title",
      "flow-subtitle",
      "flow-stat-grid",
      "flow-task-grid",
      "last-event",
      "close-logs",
      "log-dialog",
      "copy-logs",
      "log-title",
      "log-content",
    ],
    locationPathname: "/flows/shayan",
    apiResolver(path) {
      if (path === "/api/flows/shayan?limit_per_task=20") {
        return JSON.parse(JSON.stringify(payload));
      }
      if (path === "/api/tasks/shayan.quick/toggle") {
        return { action: "start" };
      }
      if (path === "/api/system/stop-all") {
        return { action: "stop_all_graceful" };
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  assert.equal(harness.elements.get("flow-title").textContent, "Shayan");
  assert.match(harness.elements.get("flow-task-grid").innerHTML, /Quick run completed/);
  assert.match(harness.elements.get("flow-task-grid").innerHTML, /\/tasks\/quick/);

  const before = harness.apiCalls.filter((call) => call.path === "/api/flows/shayan?limit_per_task=20").length;
  harness.sse.config.onEvent({ type: "task.completed" }, { lastEventId: "9" });
  await harness.timer.runAllTimeouts();
  await harness.flush();
  const after = harness.apiCalls.filter((call) => call.path === "/api/flows/shayan?limit_per_task=20").length;
  assert.ok(after > before);
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

test("library classifications page renders API error state", async () => {
  const harness = createHarness({
    source: LIBRARY_CLASSIFICATIONS_SOURCE,
    ids: CLASSIFICATIONS_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver(path) {
      if (path.startsWith("/api/library/")) {
        throw new Error("classifications unavailable");
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(
    harness.elements.get("classification-table-status").textContent,
    /Classifications unavailable/,
  );
  assert.match(harness.elements.get("tree-root").innerHTML, /classifications unavailable/);
  assert.match(
    harness.elements.get("normalization-status").textContent,
    /classifications unavailable/,
  );
});

function createClassificationsResolver({ malicious = false } = {}) {
  return (path) => {
    if (path.startsWith("/api/library/classifications?")) {
      return {
        available: true,
        page: 1,
        total_pages: 1,
        total: 1,
        items: [
          {
            classification_id: malicious ? '1" onclick="alert(1)' : 1,
            ddc: malicious ? '<img src=x onerror=alert(1)>' : "891.7",
            path: malicious ? '<script>alert("x")</script>' : "Language / Tatar",
            usage_count: 5,
            status: "active",
            created_by: malicious ? "<b>seed</b>" : "seed",
            created_at: "2026-03-24T12:00:00Z",
          },
        ],
      };
    }
    if (path === "/api/library/classifications/insights") {
      return {
        available: true,
        tree: [],
        distribution: [],
        duplicates: [
          {
            path: malicious ? '<svg onload=alert(1)>' : "Language / Tatar",
            issue: "duplicate_path",
            total_usage: 2,
            distinct_ddc_count: 2,
            items: [
              {
                classification_id: malicious ? '2" onclick="alert(2)' : 2,
                ddc: malicious ? "<iframe>" : "891.7",
                usage_count: 2,
              },
            ],
          },
        ],
        unclassified_queue: { total: 0, items: [] },
      };
    }
    if (path.startsWith("/api/library/classifications/normalization-preview?")) {
      return {
        available: true,
        rules: { drop_segments: ["Turkic literature"] },
        summary: {
          total_rows_scanned: 1,
          affected_classifications: 1,
          estimated_reassigned_documents: 1,
          merge_group_candidates: 0,
        },
        merge_groups: [],
        affected_preview: [
          {
            classification_id: malicious ? "3<script>" : 3,
            original_path: malicious ? "<script>orig</script>" : "orig",
            normalized_path: malicious ? "<img src=x>" : "norm",
            usage_count: 1,
          },
        ],
      };
    }
    if (path.startsWith("/api/library/classifications/merge-candidates?")) {
      return {
        available: true,
        summary: { candidate_count: 1, rows_scanned: 1, min_score: 0.8 },
        candidates: [
          {
            issue: "duplicate_path",
            score: 0.9,
            impact: 1,
            recommended_primary_classification_id: 10,
            primary: {
              classification_id: malicious ? '10" onclick="alert(3)' : 10,
              ddc: malicious ? "<script>p</script>" : "891.7",
              path: malicious ? "<img src=x>" : "path-primary",
              usage_count: 1,
            },
            secondary: {
              classification_id: malicious ? '11" onclick="alert(4)' : 11,
              ddc: malicious ? "<script>s</script>" : "891.8",
              path: "path-secondary",
              usage_count: 1,
            },
          },
        ],
      };
    }
    if (path === "/api/library") {
      return {
        global: { active_tasks: 0, active_workflows: 0, stop_all_state: "disabled" },
      };
    }
    throw new Error(`unexpected path: ${path}`);
  };
}

function createPersonalitiesResolver({
  summary = null,
} = {}) {
  return (path) => {
    if (path === "/api/library/personalities") {
      return {
        global: { active_tasks: 0, active_workflows: 0, stop_all_state: "disabled" },
        overview: {
          available: true,
          config_source: "test",
          stats: {
            total_mentions: 3,
            docs_with_authors: 2,
            unique_raw_names: 2,
            unique_normalized_names: 2,
            mixed_script_mentions: 0,
            patronymic_mentions: 0,
          },
          top_personalities: [],
        },
      };
    }
    if (path.startsWith("/api/library/personalities/table?")) {
      return {
        available: true,
        page: 1,
        total_pages: 1,
        total: 1,
        items: [
          {
            raw_name: "Alias One",
            normalized_name: "alias one",
            script_label: "latin",
            docs_count: 1,
            mentions_count: 1,
            patronymic_mentions: 0,
          },
        ],
      };
    }
    if (path.startsWith("/api/library/personalities/insights")) {
      return {
        available: true,
        script_distribution: [{ script_label: "latin", mentions_count: 1, share_pct: 100 }],
        variant_clusters: [
          {
            normalized_name: "alias one",
            variants_count: 1,
            docs_count: 1,
            mentions_count: 1,
            variants: [],
          },
        ],
        ambiguous_queue: {
          total: 1,
          items: [{ raw_name: "Alias One", script_label: "latin", reasons: ["manual_review"], docs_count: 1 }],
        },
        summary: summary || {
          script_total_mentions: 1,
          variant_cluster_count: 1,
          ambiguous_queue_total: 1,
        },
      };
    }
    if (path === "/api/system/stop-all") return { action: "stop_all_graceful" };
    throw new Error(`unexpected path: ${path}`);
  };
}

function createPublishersResolver({
  summary = null,
} = {}) {
  return (path) => {
    if (path === "/api/library/publishers") {
      return {
        global: { active_tasks: 0, active_workflows: 0, stop_all_state: "disabled" },
        overview: {
          available: true,
          config_source: "test",
          stats: {
            total_mentions: 3,
            docs_with_publishers: 2,
            unique_raw_names: 2,
            unique_normalized_names: 2,
            mixed_script_mentions: 0,
            org_marker_mentions: 0,
          },
          top_publishers: [],
        },
      };
    }
    if (path.startsWith("/api/library/publishers/table?")) {
      return {
        available: true,
        page: 1,
        total_pages: 1,
        total: 1,
        items: [
          {
            raw_name: "Publisher One",
            normalized_name: "publisher one",
            script_label: "latin",
            docs_count: 1,
            mentions_count: 1,
            org_marker_mentions: 0,
          },
        ],
      };
    }
    if (path.startsWith("/api/library/publishers/insights")) {
      return {
        available: true,
        script_distribution: [{ script_label: "latin", mentions_count: 1, share_pct: 100 }],
        variant_clusters: [
          {
            normalized_name: "publisher one",
            variants_count: 1,
            docs_count: 1,
            mentions_count: 1,
            variants: [],
          },
        ],
        ambiguous_queue: {
          total: 1,
          items: [{ raw_name: "Publisher One", script_label: "latin", reasons: ["manual_review"], docs_count: 1 }],
        },
        summary: summary || {
          script_total_mentions: 1,
          variant_cluster_count: 1,
          ambiguous_queue_total: 1,
        },
      };
    }
    if (path === "/api/system/stop-all") return { action: "stop_all_graceful" };
    throw new Error(`unexpected path: ${path}`);
  };
}

function createCollectionsResolver({
  summary = null,
} = {}) {
  return (path) => {
    if (path === "/api/library/collections") {
      return {
        global: { active_tasks: 0, active_workflows: 0, stop_all_state: "disabled" },
        overview: {
          available: true,
          config_source: "test",
          stats: {
            total_collections: 3,
            approved_collections: 1,
            included_collections: 1,
            suggested_collections: 2,
            items_linked: 14,
          },
          top_collections: [],
        },
      };
    }
    if (path.startsWith("/api/library/collections/table?")) {
      return {
        available: true,
        page: 1,
        total_pages: 1,
        total: 2,
        items: [
          {
            collection_id: 11,
            title: "Collection One",
            normalized_title: "collection one",
            status: "suggested",
            include_in_library: true,
            confidence: 0.81,
            item_count: 3,
            last_detected_at: "2026-03-25T12:00:00Z",
          },
        ],
      };
    }
    if (path === "/api/library/collections/insights") {
      return {
        available: true,
        summary: summary || {
          cluster_count: 7,
          queue_total: 5,
        },
        clusters: [
          {
            title: "Collection One",
            status: "suggested",
            item_count: 3,
            confidence: 0.81,
          },
        ],
        queue: {
          total: 1,
          items: [
            {
              collection_id: 11,
              title: "Collection One",
              status: "suggested",
              include_in_library: true,
              confidence: 0.81,
              item_count: 3,
            },
          ],
        },
      };
    }
    if (path.startsWith("/api/library/collections/11/items")) {
      return {
        available: true,
        collection_id: 11,
        items: [
          {
            md5: "abc123",
            item_title: "Issue #1",
            ya_path: "/path/issue-1.pdf",
            lib: false,
          },
        ],
      };
    }
    if (path === "/api/system/stop-all") return { action: "stop_all_graceful" };
    throw new Error(`unexpected path: ${path}`);
  };
}

test("library classifications page escapes dangerous strings in rendered html", async () => {
  const harness = createHarness({
    source: LIBRARY_CLASSIFICATIONS_SOURCE,
    ids: CLASSIFICATIONS_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver: createClassificationsResolver({ malicious: true }),
  });
  await harness.flush();

  const tableHtml = harness.elements.get("classification-table-body").innerHTML;
  const duplicatesHtml = harness.elements.get("duplicates-root").innerHTML;
  const normalizationHtml = harness.elements.get("normalization-affected").innerHTML;
  const mergeHtml = harness.elements.get("merge-root").innerHTML;
  const combined = `${tableHtml}\n${duplicatesHtml}\n${normalizationHtml}\n${mergeHtml}`;

  assert.equal(combined.includes("<img"), false);
  assert.equal(combined.includes("<script"), false);
  assert.equal(combined.includes("onclick="), false);
  assert.match(combined, /&lt;img/);
  assert.match(combined, /&lt;script/);
});

test("library personalities page renders API error state", async () => {
  const harness = createHarness({
    source: LIBRARY_PERSONALITIES_SOURCE,
    ids: PERSONALITIES_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver(path) {
      if (path.startsWith("/api/library/personalities")) {
        throw new Error("personalities unavailable");
      }
      if (path === "/api/system/stop-all") return { action: "stop_all_graceful" };
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(
    harness.elements.get("personality-status").textContent,
    /Personalities unavailable/,
  );
  assert.match(
    harness.elements.get("personality-table-status").textContent,
    /personalities unavailable/,
  );
  assert.match(harness.elements.get("scripts-root").innerHTML, /personalities unavailable/);
});

test("library personalities page prefers backend summary counters for badges", async () => {
  const harness = createHarness({
    source: LIBRARY_PERSONALITIES_SOURCE,
    ids: PERSONALITIES_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver: createPersonalitiesResolver({
      summary: {
        script_total_mentions: 91,
        variant_cluster_count: 81,
        ambiguous_queue_total: 71,
      },
    }),
  });
  await harness.flush();
  assert.equal(harness.elements.get("tab-badge-scripts").textContent, "91");
  assert.equal(harness.elements.get("tab-badge-clusters").textContent, "81");
  assert.equal(harness.elements.get("tab-badge-queue").textContent, "71");
});

test("library publishers page renders API error state", async () => {
  const harness = createHarness({
    source: LIBRARY_PUBLISHERS_SOURCE,
    ids: PUBLISHERS_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver(path) {
      if (path.startsWith("/api/library/publishers")) {
        throw new Error("publishers unavailable");
      }
      if (path === "/api/system/stop-all") return { action: "stop_all_graceful" };
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(
    harness.elements.get("publisher-status").textContent,
    /Publishers unavailable/,
  );
  assert.match(
    harness.elements.get("publisher-table-status").textContent,
    /publishers unavailable/,
  );
  assert.match(harness.elements.get("scripts-root").innerHTML, /publishers unavailable/);
});

test("library publishers page prefers backend summary counters for badges", async () => {
  const harness = createHarness({
    source: LIBRARY_PUBLISHERS_SOURCE,
    ids: PUBLISHERS_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver: createPublishersResolver({
      summary: {
        script_total_mentions: 51,
        variant_cluster_count: 41,
        ambiguous_queue_total: 31,
      },
    }),
  });
  await harness.flush();
  assert.equal(harness.elements.get("tab-badge-scripts").textContent, "51");
  assert.equal(harness.elements.get("tab-badge-clusters").textContent, "41");
  assert.equal(harness.elements.get("tab-badge-queue").textContent, "31");
});

test("library collections page renders API error state", async () => {
  const harness = createHarness({
    source: LIBRARY_COLLECTIONS_SOURCE,
    ids: COLLECTIONS_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver(path) {
      if (path.startsWith("/api/library/collections")) {
        throw new Error("collections unavailable");
      }
      if (path === "/api/system/stop-all") return { action: "stop_all_graceful" };
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(
    harness.elements.get("collections-status").textContent,
    /Collections unavailable/,
  );
  assert.match(
    harness.elements.get("collections-table-status").textContent,
    /collections unavailable/,
  );
  assert.match(harness.elements.get("clusters-root").innerHTML, /collections unavailable/);
});

test("library collections page prefers backend summary counters for badges", async () => {
  const harness = createHarness({
    source: LIBRARY_COLLECTIONS_SOURCE,
    ids: COLLECTIONS_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver: createCollectionsResolver({
      summary: {
        cluster_count: 37,
        queue_total: 27,
      },
    }),
  });
  await harness.flush();
  assert.equal(harness.elements.get("tab-badge-clusters").textContent, "37");
  assert.equal(harness.elements.get("tab-badge-queue").textContent, "27");
});

test("library classification detail page renders API error state", async () => {
  const harness = createHarness({
    source: LIBRARY_CLASSIFICATION_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "last-event",
      "classification-status",
      "classification-title",
      "classification-stat-grid",
      "linked-docs-body",
      "language-root",
      "meta-runs-root",
      "docs-page-label",
      "docs-prev",
      "docs-next",
    ],
    locationPathname: "/library/classifications/42",
    apiResolver(path) {
      if (path.startsWith("/api/library/classifications/42?")) {
        throw new Error("classification detail unavailable");
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(
    harness.elements.get("classification-status").textContent,
    /Classification unavailable/,
  );
  assert.equal(harness.elements.get("classification-title").textContent, "Classification");
});

test("library classification detail escapes dangerous strings in stats", async () => {
  const harness = createHarness({
    source: LIBRARY_CLASSIFICATION_SOURCE,
    ids: [
      "global-status",
      "stop-all-btn",
      "last-event",
      "classification-status",
      "classification-title",
      "classification-stat-grid",
      "linked-docs-body",
      "language-root",
      "meta-runs-root",
      "docs-page-label",
      "docs-prev",
      "docs-next",
    ],
    locationPathname: "/library/classifications/42",
    apiResolver(path) {
      if (path.startsWith("/api/library/classifications/42?")) {
        return {
          global: { active_tasks: 0, active_workflows: 0, stop_all_state: "disabled" },
          detail: {
            available: true,
            config_source: "<script>cfg</script>",
            classification: {
              classification_id: '42" onmouseover="alert(1)',
              ddc: "<img src=x onerror=alert(1)>",
              usage_count: "<script>1</script>",
              status: "<b>active</b>",
              path: "<script>path</script>",
              path_tt: "<script>path-tt</script>",
              created_by: "<img src=x>",
              created_at: "2026-03-24T12:00:00Z",
            },
            linked_docs: { items: [], page: 1, total_pages: 1 },
            language_distribution: [],
          },
          recent_meta_evaluate_runs: [],
        };
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  const html = harness.elements.get("classification-stat-grid").innerHTML;
  assert.equal(html.includes("<img"), false);
  assert.equal(html.includes("<script"), false);
  assert.equal(html.includes("onmouseover="), false);
  assert.match(html, /&lt;img/);
  assert.match(html, /&lt;script/);
});

test("library normalization page renders API error state", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    locationPathname: "/library/normalization/personality",
    apiResolver(path) {
      if (path.startsWith("/api/library/normalization/personality")) {
        throw new Error("normalization unavailable");
      }
      if (path === "/api/system/stop-all") return { action: "stop_all_graceful" };
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  assert.match(
    harness.elements.get("normalization-status").textContent,
    /Normalization unavailable/,
  );
  assert.match(
    harness.elements.get("queue-status").textContent,
    /Queue unavailable|normalization unavailable/,
  );
});

function createNormalizationResolver({
  stopAllState = "normal",
  suggestionsItems = [],
  mergeItems = [],
  historyItems = [],
} = {}) {
  return (path) => {
    if (path === "/api/library/normalization/personality") {
      return {
        global: { active_tasks: 1, active_workflows: 0, stop_all_state: stopAllState },
        dashboard: {
          available: true,
          config_source: "test",
          stats: {
            total_aliases: 10,
            docs_with_entities: 7,
            canonicals: 2,
            linked: 6,
            unreviewed: 4,
            suggested: 3,
            coverage_pct: 60,
          },
          suggestions: { open_total: 3 },
        },
      };
    }
    if (path.startsWith("/api/library/normalization/personality/canonicals?")) {
      return { available: true, items: [{ canonical_id: 1, display_name: "Author One", normalized_name: "author one", linked_aliases: 2, status: "active", notes: "" }] };
    }
    if (path === "/api/library/normalization/personality/canonicals") {
      return { accepted: true };
    }
    if (path.startsWith("/api/library/normalization/personality/queue?")) {
      const page = Number(new URL(`http://local${path}`).searchParams.get("page") || "1");
      return {
        available: true,
        page,
        total_pages: 2,
        total: 2,
        items: [
          {
            raw_name: page === 1 ? "Alias One" : "Alias Two",
            normalized_name: page === 1 ? "alias one" : "alias two",
            script_label: "latin",
            docs_count: 1,
            mentions_count: 1,
            queue_status: "pending",
            canonical_id: null,
            canonical_name: null,
            suggestion: null,
          },
        ],
      };
    }
    if (path.startsWith("/api/library/normalization/personality/suggestions?")) {
      return { available: true, items: suggestionsItems };
    }
    if (path.startsWith("/api/library/normalization/personality/merge-candidates?")) {
      return {
        available: true,
        summary: { candidate_count: mergeItems.length },
        items: mergeItems,
      };
    }
    if (path === "/api/library/normalization/personality/quality") {
      return { available: true, stats: { total_aliases: 10, linked_aliases: 6, rejected_aliases: 0, unresolved_aliases: 4, unresolved_docs_estimate: 4, duplicate_normalized_keys: 0, coverage_pct: 60 } };
    }
    if (path === "/api/library/normalization/personality/history?limit=200") {
      return { available: true, items: historyItems };
    }
    if (path === "/api/system/stop-all") {
      return { action: "stop_all_force" };
    }
    if (path === "/api/library/normalization/personality/suggestions/refresh") {
      return { accepted: true };
    }
    if (path.startsWith("/api/library/normalization/personality/evidence?")) {
      return {
        available: true,
        items: [
          {
            md5: "abc123",
            language: "tt",
            ya_path: "/library/file.pdf",
            document_url: "https://example.test/doc",
            content_url: "https://example.test/content",
          },
        ],
      };
    }
    if (path === "/api/library/normalization/personality/bulk/link") {
      return { accepted: true };
    }
    if (path === "/api/library/normalization/personality/bulk/reject") {
      return { accepted: true };
    }
    if (path === "/api/library/normalization/personality/decisions/link") {
      return { accepted: true };
    }
    if (path === "/api/library/normalization/personality/decisions/reject") {
      return { accepted: true };
    }
    if (path === "/api/library/normalization/personality/decisions/create-link") {
      return { accepted: true };
    }
    if (path === "/api/library/normalization/personality/merge") {
      return { accepted: true };
    }
    if (/^\/api\/library\/normalization\/personality\/history\/\d+\/undo$/.test(path)) {
      return { accepted: true };
    }
    throw new Error(`unexpected path: ${path}`);
  };
}

test("library normalization queue pagination requests next page", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver(),
  });
  await harness.flush();

  harness.elements.get("queue-page-next").dispatch("click");
  await harness.flush();

  const queueCalls = harness.apiCalls
    .map((call) => call.path)
    .filter((path) => path.startsWith("/api/library/normalization/personality/queue?"));
  const hasPage2 = queueCalls.some((path) => new URL(`http://local${path}`).searchParams.get("page") === "2");
  assert.equal(hasPage2, true);
});

test("library normalization stop-all respects force confirmation", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    confirmResult: false,
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver({ stopAllState: "armed" }),
  });
  await harness.flush();

  harness.elements.get("stop-all-btn").dispatch("click");
  await harness.flush();

  const stopCalls = harness.apiCalls.filter((call) => call.path === "/api/system/stop-all");
  assert.equal(stopCalls.length, 0);
});

test("library normalization suggestions refresh posts configured payload", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver(),
  });
  await harness.flush();
  harness.elements.get("suggestions-limit").value = "42";
  harness.elements.get("suggestions-use-gemini").checked = true;

  harness.elements.get("suggestions-refresh-btn").dispatch("click");
  await harness.flush();

  const refreshCall = harness.apiCalls.find(
    (call) => call.path === "/api/library/normalization/personality/suggestions/refresh",
  );
  assert.equal(Boolean(refreshCall), true);
  assert.equal(refreshCall.options.method, "POST");
  const body = JSON.parse(refreshCall.options.body || "{}");
  assert.equal(body.limit, 42);
  assert.equal(body.use_gemini, true);
});

test("library normalization bulk link posts selected aliases and canonical id", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select", ".queue-row-select:checked"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver(),
  });
  await harness.flush();

  harness.elements.get("queue-bulk-canonical").value = "1";
  harness.elements.get("selectorAll:.queue-row-select:checked").dataset.raw = encodeURIComponent(
    "Alias One",
  );
  harness.elements.get("queue-bulk-link").dispatch("click");
  await harness.flush();

  const call = harness.apiCalls.find(
    (entry) => entry.path === "/api/library/normalization/personality/bulk/link",
  );
  assert.equal(Boolean(call), true);
  assert.equal(call.options.method, "POST");
  const body = JSON.parse(call.options.body || "{}");
  assert.equal(body.canonical_id, 1);
  assert.deepEqual(body.raw_names, ["Alias One"]);
});

test("library normalization bulk reject posts selected aliases", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select", ".queue-row-select:checked"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver(),
  });
  await harness.flush();

  harness.elements.get("selectorAll:.queue-row-select:checked").dataset.raw = encodeURIComponent(
    "Alias One",
  );
  harness.elements.get("queue-bulk-reject").dispatch("click");
  await harness.flush();

  const call = harness.apiCalls.find(
    (entry) => entry.path === "/api/library/normalization/personality/bulk/reject",
  );
  assert.equal(Boolean(call), true);
  assert.equal(call.options.method, "POST");
  const body = JSON.parse(call.options.body || "{}");
  assert.deepEqual(body.raw_names, ["Alias One"]);
});

test("library normalization canonical create posts payload and clears input", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver(),
  });
  await harness.flush();

  harness.elements.get("canonical-create-name").value = "Author New";
  harness.elements.get("canonical-create-notes").value = "manual seed";
  harness.elements.get("canonical-create-btn").dispatch("click");
  await harness.flush();

  const createCall = harness.apiCalls.find(
    (entry) => entry.path === "/api/library/normalization/personality/canonicals",
  );
  assert.equal(Boolean(createCall), true);
  assert.equal(createCall.options.method, "POST");
  const body = JSON.parse(createCall.options.body || "{}");
  assert.equal(body.display_name, "Author New");
  assert.equal(body.notes, "manual seed");
  assert.equal(harness.elements.get("canonical-create-name").value, "");
});

test("library normalization canonical search apply sends search query", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver(),
  });
  await harness.flush();

  harness.elements.get("canonical-search").value = "Author";
  harness.elements.get("canonical-search-apply").dispatch("click");
  await harness.flush();

  const canonicalCalls = harness.apiCalls
    .map((entry) => entry.path)
    .filter((path) => path.startsWith("/api/library/normalization/personality/canonicals?"));
  const hasSearch = canonicalCalls.some(
    (path) => new URL(`http://local${path}`).searchParams.get("search") === "Author",
  );
  assert.equal(hasSearch, true);
});

test("library normalization queue create action posts create-link decision", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    promptResult: "Author Via Prompt",
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver(),
  });
  await harness.flush();

  harness.elements.get("queue-table-body").dispatch("click", {
    target: {
      closest(selector) {
        if (selector !== ".queue-action-btn") return null;
        return {
          dataset: {
            action: "create",
            raw: encodeURIComponent("Alias One"),
          },
        };
      },
    },
  });
  await harness.flush();

  assert.equal(harness.prompts.length > 0, true);
  assert.equal(harness.prompts[0].defaultValue, "Alias One");
  const call = harness.apiCalls.find(
    (entry) => entry.path === "/api/library/normalization/personality/decisions/create-link",
  );
  assert.equal(Boolean(call), true);
  assert.equal(call.options.method, "POST");
  const body = JSON.parse(call.options.body || "{}");
  assert.equal(body.raw_name, "Alias One");
  assert.equal(body.display_name, "Author Via Prompt");
  assert.deepEqual(body.suggestion_ids, []);
});

test("library normalization suggestion accept posts link decision", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver({
      suggestionsItems: [
        {
          raw_name: "Alias One",
          suggestion_id: 11,
          suggestion_kind: "link",
          target_canonical_id: 1,
          target_canonical_name: "Author One",
          confidence: 0.95,
          confidence_band: "high",
          rationale: "exact",
        },
      ],
    }),
  });
  await harness.flush();

  harness.elements.get("suggestions-table-body").dispatch("click", {
    target: {
      closest(selector) {
        if (selector !== ".suggestion-action-btn") return null;
        return {
          dataset: {
            action: "accept",
            raw: encodeURIComponent("Alias One"),
            suggestionId: "11",
          },
        };
      },
    },
  });
  await harness.flush();

  const call = harness.apiCalls.find(
    (entry) => entry.path === "/api/library/normalization/personality/decisions/link",
  );
  assert.equal(Boolean(call), true);
  const body = JSON.parse(call.options.body || "{}");
  assert.equal(body.raw_name, "Alias One");
  assert.equal(body.canonical_id, 1);
  assert.deepEqual(body.suggestion_ids, [11]);
});

test("library normalization suggestion reject posts reject decision", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver({
      suggestionsItems: [
        {
          raw_name: "Alias One",
          suggestion_id: 12,
          suggestion_kind: "link",
          target_canonical_id: 1,
          confidence: 0.91,
          confidence_band: "high",
          rationale: "similar",
        },
      ],
    }),
  });
  await harness.flush();

  harness.elements.get("suggestions-table-body").dispatch("click", {
    target: {
      closest(selector) {
        if (selector !== ".suggestion-action-btn") return null;
        return {
          dataset: {
            action: "reject",
            raw: encodeURIComponent("Alias One"),
            suggestionId: "12",
          },
        };
      },
    },
  });
  await harness.flush();

  const call = harness.apiCalls.find(
    (entry) => entry.path === "/api/library/normalization/personality/decisions/reject",
  );
  assert.equal(Boolean(call), true);
  const body = JSON.parse(call.options.body || "{}");
  assert.equal(body.raw_name, "Alias One");
  assert.deepEqual(body.suggestion_ids, [12]);
});

test("library normalization merge action posts merge request", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver({
      mergeItems: [
        {
          left: { canonical_id: 1, display_name: "Author One" },
          right: { canonical_id: 2, display_name: "Author Two" },
          recommended_primary_canonical_id: 1,
          score: 0.93,
          impact: 10,
        },
      ],
    }),
  });
  await harness.flush();

  harness.elements.get("merge-root").dispatch("click", {
    target: {
      closest(selector) {
        if (selector !== ".merge-apply-btn") return null;
        return { dataset: { sourceId: "2", targetId: "1" } };
      },
    },
  });
  await harness.flush();

  const call = harness.apiCalls.find(
    (entry) => entry.path === "/api/library/normalization/personality/merge",
  );
  assert.equal(Boolean(call), true);
  const body = JSON.parse(call.options.body || "{}");
  assert.equal(body.source_canonical_id, 2);
  assert.equal(body.target_canonical_id, 1);
});

test("library normalization history undo posts undo request", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver({
      historyItems: [
        {
          event_id: 77,
          action: "link_alias",
          payload: { raw_name: "Alias One", canonical_id: 1 },
          created_at: "2026-03-24T10:00:00Z",
          reverted: false,
        },
      ],
    }),
  });
  await harness.flush();

  harness.elements.get("history-root").dispatch("click", {
    target: {
      closest(selector) {
        if (selector !== ".history-undo-btn") return null;
        return { dataset: { eventId: "77" } };
      },
    },
  });
  await harness.flush();

  const call = harness.apiCalls.find(
    (entry) => entry.path === "/api/library/normalization/personality/history/77/undo",
  );
  assert.equal(Boolean(call), true);
  assert.equal(call.options.method, "POST");
});

test("library normalization suggestion queue action switches context and filters queue", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver({
      suggestionsItems: [
        {
          raw_name: "Alias One",
          suggestion_id: 15,
          suggestion_kind: "link",
          target_canonical_id: 1,
          confidence: 0.88,
          confidence_band: "medium",
          rationale: "normalized_match",
        },
      ],
    }),
  });
  await harness.flush();

  const tabSwitcher = harness.elements.get("selectorAll:.classification-tab");
  tabSwitcher.setAttribute("data-tab", "suggestions");
  tabSwitcher.dispatch("click");
  await harness.flush();

  harness.elements.get("suggestions-table-body").dispatch("click", {
    target: {
      closest(selector) {
        if (selector !== ".suggestion-action-btn") return null;
        return {
          dataset: {
            action: "queue",
            raw: encodeURIComponent("Alias One"),
            suggestionId: "15",
          },
        };
      },
    },
  });
  await harness.flush();

  assert.equal(harness.elements.get("queue-filter-search").value, "Alias One");
  assert.equal(harness.elements.get("tab-btn-queue").classList.contains("active"), true);
  const queueCalls = harness.apiCalls
    .map((entry) => entry.path)
    .filter((path) => path.startsWith("/api/library/normalization/personality/queue?"));
  const hasSearchAlias = queueCalls.some(
    (path) => new URL(`http://local${path}`).searchParams.get("search") === "Alias One",
  );
  assert.equal(hasSearchAlias, true);
});

test("library normalization evidence action opens dialog and loads evidence text", async () => {
  const harness = createHarness({
    source: LIBRARY_NORMALIZATION_SOURCE,
    ids: NORMALIZATION_PAGE_IDS,
    selectors: [".classification-tab", ".queue-row-select"],
    locationPathname: "/library/normalization/personality",
    apiResolver: createNormalizationResolver(),
  });
  await harness.flush();

  harness.elements.get("queue-table-body").dispatch("click", {
    target: {
      closest(selector) {
        if (selector !== ".queue-action-btn") return null;
        return {
          dataset: {
            action: "evidence",
            raw: encodeURIComponent("Alias One"),
          },
        };
      },
    },
  });
  await harness.flush();

  assert.equal(harness.elements.get("evidence-dialog").open, true);
  assert.match(harness.elements.get("evidence-title").textContent, /Alias Evidence: Alias One/);
  assert.match(harness.elements.get("evidence-content").textContent, /md5=abc123/);
  const evidenceCall = harness.apiCalls.find((entry) =>
    entry.path.startsWith("/api/library/normalization/personality/evidence?"),
  );
  assert.equal(Boolean(evidenceCall), true);
});
