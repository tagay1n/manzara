import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

export const TASKS_SOURCE = readFileSync(new URL("../../../static/tasks.js", import.meta.url), "utf-8");
const TASK_REVIEW_STATE_SOURCE = readFileSync(
  new URL("../../../static/task-review-state.js", import.meta.url),
  "utf-8",
);
const CONVEYOR_SOURCE = readFileSync(new URL("../../../static/conveyor.js", import.meta.url), "utf-8");
export const TASKS_PAGE_SOURCE = [TASK_REVIEW_STATE_SOURCE, CONVEYOR_SOURCE, TASKS_SOURCE].join("\n");
export const TASK_SOURCE = [
  TASK_REVIEW_STATE_SOURCE,
  readFileSync(new URL("../../../static/task.js", import.meta.url), "utf-8"),
].join("\n");
export const DASHBOARD_SOURCE = readFileSync(new URL("../../../static/app.js", import.meta.url), "utf-8");
export const LIBRARY_SOURCE = readFileSync(new URL("../../../static/library.js", import.meta.url), "utf-8");
export const DATABASE_SOURCE = readFileSync(new URL("../../../static/database.js", import.meta.url), "utf-8");
export const GEMINI_SOURCE = readFileSync(new URL("../../../static/gemini.js", import.meta.url), "utf-8");
export const LIBRARY_CLASSIFICATIONS_SOURCE = readFileSync(
  new URL("../../../static/library-classifications.js", import.meta.url),
  "utf-8",
);
export const LIBRARY_PERSONALITIES_SOURCE = readFileSync(
  new URL("../../../static/library-entities.js", import.meta.url),
  "utf-8",
);
export const LIBRARY_PUBLISHERS_SOURCE = readFileSync(
  new URL("../../../static/library-entities.js", import.meta.url),
  "utf-8",
);
export const LIBRARY_COLLECTIONS_SOURCE = readFileSync(
  new URL("../../../static/library-collections.js", import.meta.url),
  "utf-8",
);
export const LIBRARY_DOCUMENT_CLEANUP_SOURCE = readFileSync(
  new URL("../../../static/library-document-cleanup.js", import.meta.url),
  "utf-8",
);
export const LIBRARY_CLASSIFICATION_SOURCE = readFileSync(
  new URL("../../../static/library-classification.js", import.meta.url),
  "utf-8",
);
export const LIBRARY_NORMALIZATION_SOURCE = [
  readFileSync(
    new URL("../../../static/library-normalization-rendering.js", import.meta.url),
    "utf-8",
  ),
  readFileSync(
    new URL("../../../static/library-normalization.js", import.meta.url),
    "utf-8",
  ),
].join("\n");

export const NORMALIZATION_PAGE_IDS = [
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

export const GEMINI_PAGE_IDS = [
  "global-status",
  "last-event",
  "gemini-status",
  "gemini-stat-grid",
  "gemini-model-usage",
  "gemini-accounts",
  "reset-all-btn",
  "override-blackout-btn",
];

export const CLASSIFICATIONS_PAGE_IDS = [
  "global-status",
  "stop-all-btn",
  "last-event",
  "classification-table-body",
  "classification-table-status",
  "distribution-root",
  "duplicates-status",
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

export const PERSONALITIES_PAGE_IDS = [
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

export const PUBLISHERS_PAGE_IDS = [
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

export const COLLECTIONS_PAGE_IDS = [
  "global-status",
  "stop-all-btn",
  "last-event",
  "collections-status",
  "collections-stat-grid",
  "collections-list-status",
  "collections-list-root",
  "page-label",
  "page-prev",
  "page-next",
  "filter-search",
  "filter-apply",
];

export const DOCUMENT_CLEANUP_PAGE_IDS = [
  "cleanup-stat-grid",
  "cleanup-status",
  "cleanup-list",
  "last-event",
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

export function createHarness({
  source,
  ids,
  selectors = [],
  apiResolver,
  confirmResult = true,
  promptResult = null,
  locationPathname = "/tasks",
  localStorageEntries = {},
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
  const logViewer = {
    activeRunId: null,
    closeCalls: 0,
    openCalls: [],
  };
  const storedValues = new Map(
    Object.entries(localStorageEntries).map(([key, value]) => [String(key), String(value)]),
  );
  const localStorage = {
    getItem(key) {
      const value = storedValues.get(String(key));
      return value === undefined ? null : value;
    },
    setItem(key, value) {
      storedValues.set(String(key), String(value));
    },
    removeItem(key) {
      storedValues.delete(String(key));
    },
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
    ManzaraUI: {
      async confirm() {
        return confirmResult;
      },
      async prompt(options = {}) {
        prompts.push({
          message: String(options.message || ""),
          defaultValue: String(options.value || ""),
        });
        if (typeof promptResult === "function") {
          return promptResult(options);
        }
        return promptResult;
      },
      toast(message) {
        alerts.push(String(message || ""));
      },
      reportTaskActionResult(result) {
        if (result?.message) alerts.push(String(result.message));
      },
    },
    ManzaraSound: null,
    localStorage,
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
      toLucideIcon(name, fallback = "play") {
        const raw = String(name || "").trim();
        const fallbackName = String(fallback || "play").trim().toLowerCase() || "play";
        if (!raw) return fallbackName;
        const normalized = raw
          .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
          .replaceAll("_", "-")
          .replace(/\s+/g, "-")
          .toLowerCase()
          .replace(/[^a-z0-9-]+/g, "-")
          .replace(/-{2,}/g, "-")
          .replace(/^-+|-+$/g, "");
        return normalized || fallbackName;
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
      taskStatusBadgeModel(run = {}) {
        const status = String(run?.status || "idle");
        const active = this.isActiveStatus(status);
        const progress = run?.progress && typeof run.progress === "object" ? run.progress : {};
        const current = Number(progress.current);
        const total = Number(progress.total);
        const determinate = active && Number.isFinite(current) && current >= 0
          && Number.isFinite(total) && total > 0;
        const suppliedPercent = Number(progress.percent);
        const calculatedPercent = determinate ? current / total * 100 : 0;
        return {
          active,
          current,
          determinate,
          percent: Math.round(Math.max(0, Math.min(
            100,
            Number.isFinite(suppliedPercent) ? suppliedPercent : calculatedPercent,
          ))),
          status,
          total,
        };
      },
      renderTaskStatusBadge(run = {}, options = {}) {
        const status = String(run?.status || "idle");
        const active = this.isActiveStatus(status);
        const progress = run?.progress && typeof run.progress === "object" ? run.progress : {};
        const current = Number(progress.current);
        const total = Number(progress.total);
        const determinate = active && Number.isFinite(current) && current >= 0
          && Number.isFinite(total) && total > 0;
        const suppliedPercent = Number(progress.percent);
        const percent = Math.round(Math.max(0, Math.min(
          100,
          Number.isFinite(suppliedPercent) ? suppliedPercent : determinate ? current / total * 100 : 0,
        )));
        const labels = {
          idle: "Idle", starting: "Starting", running: "Running",
          stopping_graceful: "Stopping", stopping_force: "Force stopping",
          completed: "Completed", failed: "Failed", stopped: "Stopped",
        };
        const label = String(options.label || labels[status] || status);
        const classes = [
          "task-status-badge",
          `task-status-${this.cssName(status, "idle")}`,
          active ? "is-active" : "",
          status === "failed" ? "is-failed" : "",
          determinate ? "has-progress" : "",
          options.compact ? "is-compact" : "",
        ].filter(Boolean).join(" ");
        const details = determinate
          ? `<span class="task-status-progress">${current} / ${total} · ${percent}%</span>`
          : "";
        return `<span class="${classes}"><span class="task-status-label">${this.escapeHtml(label)}</span>${details}</span>`;
      },
      formatDateTime(value) {
        return `DT:${String(value)}`;
      },
      formatGlobalStatus(activeTasks) {
        return `Tasks: ${Number(activeTasks || 0)}`;
      },
      eventCursorFromSnapshot(payload) {
        const cursor = Number(payload?.event_cursor || 0);
        return Number.isFinite(cursor) && cursor > 0 ? Math.trunc(cursor) : 0;
      },
      formatEventBanner(payload) {
        return `BANNER:${String(payload?.type || "")}`;
      },
      applyStopAllButton(button, stopAllState) {
        button.dataset.stopState = String(stopAllState || "");
      },
      scheduleRefresh(state, worker, delayMs = 0) {
        if (state.refreshTimer) return;
        state.refreshTimer = timer.setTimeout(async () => {
          state.refreshTimer = null;
          await worker();
        }, delayMs);
      },
      eventNeedsReconciliation(payload) {
        const eventType = String(payload?.type || "");
        return !["task.log", "task.progress"].includes(eventType);
      },
      applyTaskEventState() {
        return false;
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
        return {
          async open(runId, taskTitle = "Task") {
            logViewer.activeRunId = Number(runId || 0);
            logViewer.openCalls.push(logViewer.activeRunId);
            if (options.titleNode) {
              options.titleNode.textContent = `Logs • ${taskTitle} • run ${logViewer.activeRunId}`;
            }
            if (options.contentNode) {
              options.contentNode.textContent = "";
            }
            if (options.dialogNode && !options.dialogNode.open) {
              options.dialogNode.showModal?.();
            }
            options.onStateChange?.({
              status: "empty",
              activeRunId: logViewer.activeRunId,
              bufferedLines: 0,
              hasMoreBefore: false,
              error: "",
            });
          },
          close(closeOptions = {}) {
            logViewer.activeRunId = null;
            logViewer.closeCalls += 1;
            if (options.contentNode && closeOptions.keepContent !== true) {
              options.contentNode.textContent = "";
            }
            if (closeOptions.closeDialog !== false && options.dialogNode?.open) {
              options.dialogNode.close?.();
            }
            options.onStateChange?.({
              status: "closed",
              activeRunId: null,
              bufferedLines: 0,
              hasMoreBefore: false,
              error: "",
            });
          },
          destroy() {
            this.close();
          },
          async loadOlder() {},
          async pollFollow() {},
          getState() {
            return {
              activeRunId: logViewer.activeRunId,
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
    logViewer,
    localStorage,
    async flush() {
      for (let i = 0; i < 16; i += 1) {
        await Promise.resolve();
      }
    },
  };
}
