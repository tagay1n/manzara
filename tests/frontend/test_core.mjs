import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const CORE_SOURCE = readFileSync(new URL("../../static/core.js", import.meta.url), "utf-8");

function loadCore({ fetchImpl, EventSourceImpl, timerApi, documentImpl } = {}) {
  const sandbox = {
    window: {},
    Intl,
    Date,
    JSON,
    Number,
    encodeURIComponent,
    URLSearchParams,
    console,
    fetch:
      fetchImpl ||
      (async () => ({
        ok: true,
        json: async () => ({}),
      })),
    EventSource:
      EventSourceImpl ||
      class {
        constructor() {}
        addEventListener() {}
        close() {}
      },
    setTimeout:
      timerApi?.setTimeout ||
      ((fn, _ms) => {
        fn();
        return 1;
      }),
    clearTimeout: timerApi?.clearTimeout || (() => {}),
    setInterval: timerApi?.setInterval || (() => 1),
    clearInterval: timerApi?.clearInterval || (() => {}),
    document:
      documentImpl ||
      {
        getElementById() {
          return null;
        },
      },
  };
  vm.createContext(sandbox);
  vm.runInContext(CORE_SOURCE, sandbox);
  return sandbox.window.ManzaraCore;
}

test("formatDateTime uses EU-style date/time with timezone and 24h clock", () => {
  const core = loadCore();
  const formatted = core.formatDateTime("2026-03-24T12:34:00Z");
  assert.match(formatted, /^\d{2}\/\d{2}\/\d{4},?\s\d{2}:\d{2}/);
  assert.equal(/AM|PM/i.test(formatted), false);
  assert.match(formatted, /(GMT|UTC)/);
});

test("formatEventBanner includes event type and timezone-aware time", () => {
  const core = loadCore();
  const text = core.formatEventBanner({
    type: "task.completed",
    ts: "2026-03-24T12:34:56Z",
  });
  assert.match(text, /^Last event: task\.completed @ /);
  assert.match(text, /(GMT|UTC)/);
});

test("applyStopAllButton renders force/graceful states", () => {
  const core = loadCore();
  const classes = new Set();
  const attrs = new Map();
  const button = {
    disabled: false,
    title: "",
    innerHTML: "",
    classList: {
      add(name) {
        classes.add(name);
      },
      remove(...names) {
        names.forEach((name) => classes.delete(name));
      },
    },
    setAttribute(name, value) {
      attrs.set(name, value);
    },
  };

  core.applyStopAllButton(button, "armed");
  assert.equal(button.disabled, false);
  assert.equal(classes.has("red"), true);
  assert.equal(button.title, "Force stop all running tasks");
  assert.match(button.innerHTML, /octagon-x/);
  assert.equal(attrs.get("aria-label"), "Force stop all running tasks");

  core.applyStopAllButton(button, "normal");
  assert.equal(button.disabled, false);
  assert.equal(classes.has("amber"), true);
  assert.equal(button.title, "Graceful stop all running tasks");
  assert.match(button.innerHTML, /square/);
  assert.equal(attrs.get("aria-label"), "Graceful stop all running tasks");

  core.applyStopAllButton(button, "disabled");
  assert.equal(button.disabled, true);
});

test("toLucideIcon normalizes camel/snake/space names and falls back", () => {
  const core = loadCore();
  assert.equal(core.toLucideIcon("RefreshCw"), "refresh-cw");
  assert.equal(core.toLucideIcon("refresh_cw"), "refresh-cw");
  assert.equal(core.toLucideIcon("Refresh Cw"), "refresh-cw");
  assert.equal(core.toLucideIcon(""), "play");
  assert.equal(core.toLucideIcon("", "square"), "square");
});

test("api returns parsed json and raises meaningful HTTP errors", async () => {
  const okCore = loadCore({
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({ ok: 1 }),
    }),
  });
  assert.deepEqual(await okCore.api("/x"), { ok: 1 });

  const failCore = loadCore({
    fetchImpl: async () => ({
      ok: false,
      status: 503,
      text: async () => "upstream unavailable",
    }),
  });
  await assert.rejects(failCore.api("/x"), /upstream unavailable/);
});

test("api extracts JSON detail from error responses", async () => {
  const core = loadCore({
    fetchImpl: async () => ({
      ok: false,
      status: 404,
      text: async () => '{"detail":"Task not found"}',
    }),
  });
  await assert.rejects(core.api("/x"), /Task not found/);
});

test("createSseController updates cursor, dispatches events, and reconnects", () => {
  const created = [];
  const scheduled = [];
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.listeners = new Map();
      this.closed = false;
      this.onopen = null;
      this.onerror = null;
      created.push(this);
    }
    addEventListener(type, handler) {
      this.listeners.set(type, handler);
    }
    emit(type, payload, lastEventId = "") {
      const handler = this.listeners.get(type);
      if (handler) {
        handler({
          data: JSON.stringify(payload),
          lastEventId: String(lastEventId),
        });
      }
    }
    close() {
      this.closed = true;
    }
  }

  let nextTimerId = 0;
  const timers = {
    setTimeout(fn, _ms) {
      const id = ++nextTimerId;
      scheduled.push({ id, fn });
      return id;
    },
    clearTimeout(_id) {},
  };

  let cursor = 7;
  const seen = [];
  const core = loadCore({ EventSourceImpl: FakeEventSource, timerApi: timers });
  const controller = core.createSseController({
    getCursor: () => cursor,
    setCursor: (next) => {
      cursor = Number(next);
    },
    onEvent: (payload) => {
      seen.push(payload.type);
    },
  });

  controller.start();
  assert.equal(created.length, 1);
  assert.equal(created[0].url, "/api/events/stream?after_event_id=7");

  created[0].emit("task.started", { type: "task.started", event_id: 8 }, "8");
  assert.deepEqual(seen, ["task.started"]);
  assert.equal(cursor, 8);

  created[0].onerror?.();
  assert.equal(scheduled.length, 1);
  scheduled[0].fn();
  assert.equal(created.length, 2);

  controller.stop();
  assert.equal(created[0].closed, true);
  assert.equal(created[1].closed, true);
});

test("API snapshot cursor prevents SSE from replaying historical events", async () => {
  const created = [];
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.listeners = new Map();
      created.push(this);
    }
    addEventListener(type, handler) {
      this.listeners.set(type, handler);
    }
    close() {}
  }

  let cursor = 0;
  const core = loadCore({
    EventSourceImpl: FakeEventSource,
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({ event_cursor: 42, flows: [] }),
    }),
  });

  const snapshot = await core.api("/api/tasks");
  const controller = core.createSseController({
    initialCursor: core.eventCursorFromSnapshot(snapshot),
    getCursor: () => cursor,
    setCursor: (next) => {
      cursor = Number(next);
    },
  });
  controller.start();

  assert.equal(cursor, 42);
  assert.equal(created[0].url, "/api/events/stream?after_event_id=42");
});

test("DEFAULT_EVENT_TYPES includes task.artifact for live artifact updates", () => {
  const core = loadCore();
  assert.equal(Array.isArray(core.DEFAULT_EVENT_TYPES), true);
  assert.equal(core.DEFAULT_EVENT_TYPES.includes("task.artifact"), true);
});

test("createTabController toggles active tab and rejects unknown tabs", () => {
  function makeNode() {
    const classes = new Set();
    const attrs = new Map();
    return {
      classes,
      attrs,
      classList: {
        toggle(name, active) {
          if (active) {
            classes.add(name);
          } else {
            classes.delete(name);
          }
        },
      },
      setAttribute(name, value) {
        attrs.set(name, String(value));
      },
    };
  }

  const nodes = new Map([
    ["tab-btn-table", makeNode()],
    ["tab-panel-table", makeNode()],
    ["tab-btn-queue", makeNode()],
    ["tab-panel-queue", makeNode()],
  ]);
  const core = loadCore({
    documentImpl: {
      getElementById(id) {
        return nodes.get(String(id)) || null;
      },
    },
  });

  let activeTab = "table";
  const controller = core.createTabController({
    tabs: ["table", "queue"],
    getActiveTab: () => activeTab,
    setActiveTab: (next) => {
      activeTab = String(next);
    },
  });

  controller.apply();
  assert.equal(nodes.get("tab-btn-table").classes.has("active"), true);
  assert.equal(nodes.get("tab-panel-table").classes.has("active"), true);
  assert.equal(nodes.get("tab-btn-table").attrs.get("aria-selected"), "true");
  assert.equal(nodes.get("tab-btn-queue").classes.has("active"), false);
  assert.equal(nodes.get("tab-btn-queue").attrs.get("aria-selected"), "false");

  assert.equal(controller.select("queue"), true);
  assert.equal(activeTab, "queue");
  assert.equal(nodes.get("tab-btn-table").classes.has("active"), false);
  assert.equal(nodes.get("tab-btn-queue").classes.has("active"), true);
  assert.equal(nodes.get("tab-btn-queue").attrs.get("aria-selected"), "true");

  assert.equal(controller.select("unknown"), false);
  assert.equal(activeTab, "queue");
});

test("setStatusMessage toggles error class and writes text", () => {
  const toggles = [];
  const node = {
    textContent: "",
    classList: {
      toggle(name, value) {
        toggles.push({ name, value: Boolean(value) });
      },
    },
  };
  const core = loadCore();
  core.setStatusMessage(node, "Loaded", { error: false });
  core.setStatusMessage(node, "Failed", { error: true });
  assert.equal(node.textContent, "Failed");
  assert.deepEqual(toggles, [
    { name: "library-status-error", value: false },
    { name: "library-status-error", value: true },
  ]);
});

test("render message helpers escape unsafe values", () => {
  const core = loadCore();
  const runRow = core.renderRunRowMessage("<script>alert(1)</script>", { error: true });
  const footnote = core.renderWorkflowFootnoteMessage("<img src=x>", { error: true });
  const loadingRow = core.renderLoadingTableRow(6, "<b>Loading</b>");
  assert.match(runRow, /Error:\s*&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(footnote, /library-status-error/);
  assert.match(footnote, /&lt;img src=x&gt;/);
  assert.match(loadingRow, /colspan="6"/);
  assert.match(loadingRow, /&lt;b&gt;Loading&lt;\/b&gt;/);
});

test("applyPaginationControls updates label and button disabled state", () => {
  const core = loadCore();
  const labelNode = { textContent: "" };
  const prevNode = { disabled: false };
  const nextNode = { disabled: false };

  core.applyPaginationControls({
    labelNode,
    prevNode,
    nextNode,
    page: 1,
    totalPages: 3,
  });
  assert.equal(labelNode.textContent, "Page 1 / 3");
  assert.equal(prevNode.disabled, true);
  assert.equal(nextNode.disabled, false);

  core.applyPaginationControls({
    labelNode,
    prevNode,
    nextNode,
    page: 3,
    totalPages: 3,
  });
  assert.equal(labelNode.textContent, "Page 3 / 3");
  assert.equal(prevNode.disabled, false);
  assert.equal(nextNode.disabled, true);
});

test("attachViewState normalizes and mutates shared state", () => {
  const core = loadCore();
  const state = {};
  const store = core.attachViewState(state, "loading");
  assert.equal(state.viewState, "loading");
  assert.equal(store.get(), "loading");
  assert.equal(store.is("loading"), true);

  store.set("ready");
  assert.equal(state.viewState, "ready");
  assert.equal(store.is("ready"), true);

  store.set("invalid");
  assert.equal(state.viewState, "ready");
});

test("createRefreshCoordinator coalesces overlap into one trailing refresh", async () => {
  const core = loadCore();
  const pending = [];
  let calls = 0;
  const coordinator = core.createRefreshCoordinator(async () => {
    calls += 1;
    await new Promise((resolve) => pending.push(resolve));
  });

  const first = coordinator.request();
  const second = coordinator.request();
  const third = coordinator.request();
  assert.equal(calls, 1);

  pending.shift()();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls, 2);

  pending.shift()();
  await Promise.all([first, second, third]);
  assert.equal(calls, 2);
  assert.equal(coordinator.getState().running, false);
});

test("event routing ignores log noise and identifies lifecycle reconciliation", () => {
  const core = loadCore();

  assert.equal(core.eventNeedsReconciliation({ type: "task.log" }), false);
  assert.equal(core.eventNeedsReconciliation({ type: "task.progress" }), false);
  assert.equal(core.eventNeedsReconciliation({ type: "task.completed" }), true);
  assert.equal(core.eventNeedsReconciliation({ type: "task.artifact" }), true);
  assert.equal(core.eventNeedsReconciliation({ type: "schedule.updated" }), false);
});

test("task progress SSE updates the matching run without frontend shadow state", () => {
  const core = loadCore();
  const root = {
    tasks: [
      {
        task_id: "shayan.upload_yadisk",
        run: { run_id: 81, status: "running" },
      },
    ],
  };

  const changed = core.applyTaskEventState(root, {
    type: "task.progress",
    task_id: "shayan.upload_yadisk",
    run_id: 81,
    payload: {
      status: "running",
      progress: {
        current: 3,
        total: 12,
        percent: 25,
        bytes_completed: 1024,
        bytes_total: 4096,
      },
    },
  });

  assert.equal(changed, true);
  assert.equal(root.tasks[0].run.progress.percent, 25);
  assert.equal(root.tasks[0].run.progress.current, 3);
});

test("task status badge exposes active determinate progress", () => {
  const core = loadCore();

  const html = core.renderTaskStatusBadge({
    status: "running",
    progress: { current: 3, total: 12, percent: 25 },
  });

  assert.match(html, /task-status-badge task-status-running is-active has-progress/);
  assert.match(html, /Running/);
  assert.match(html, /3 \/ 12/);
  assert.match(html, /25%/);
  assert.match(html, /role="progressbar"/);
  assert.match(html, /aria-valuenow="25"/);
});

test("task status badge makes failure explicit without stale progress", () => {
  const core = loadCore();

  const html = core.renderTaskStatusBadge({
    status: "failed",
    progress: { current: 9, total: 10, percent: 90 },
  });

  assert.match(html, /task-status-failed is-failed/);
  assert.match(html, />Failed</);
  assert.doesNotMatch(html, /9 \/ 10/);
  assert.doesNotMatch(html, /role="progressbar"/);
});

test("task status badge derives percent when only current and total are provided", () => {
  const core = loadCore();

  const html = core.renderTaskStatusBadge({
    status: "starting",
    progress: { current: 2, total: 8 },
  });

  assert.match(html, /2 \/ 8/);
  assert.match(html, /25%/);
});

test("late task progress does not revert a stopping task to running", () => {
  const core = loadCore();
  const root = {
    tasks: [
      {
        task_id: "shayan.upload_yadisk",
        run: { run_id: 82, status: "stopping_graceful" },
      },
    ],
  };

  core.applyTaskEventState(root, {
    type: "task.progress",
    task_id: "shayan.upload_yadisk",
    run_id: 82,
    payload: { status: "running", progress: { current: 4, total: 10, percent: 40 } },
  });

  assert.equal(root.tasks[0].run.status, "stopping_graceful");
  assert.equal(root.tasks[0].run.progress.percent, 40);
});

test("createRunLogViewer uses tail on open then follows and backfills with cursors", async () => {
  const calls = [];
  const responses = {
    tail: {
      run: { run_id: 77 },
      lines: [
        { log_id: 10, line: "line-10" },
        { log_id: 11, line: "line-11" },
        { log_id: 12, line: "line-12" },
      ],
      next_after_log_id: 12,
      next_before_log_id: 10,
      has_more_before: true,
    },
    follow: {
      run: { run_id: 77 },
      lines: [{ log_id: 13, line: "line-13" }],
      next_after_log_id: 13,
      next_before_log_id: 10,
      has_more_before: true,
    },
    backfill: {
      run: { run_id: 77 },
      lines: [{ log_id: 9, line: "line-09" }],
      next_after_log_id: 9,
      next_before_log_id: 9,
      has_more_before: false,
    },
  };

  const fakeApi = async (path) => {
    calls.push(path);
    if (path.includes("tail=true")) return responses.tail;
    if (path.includes("after_log_id=12")) return responses.follow;
    if (path.includes("before_log_id=10")) return responses.backfill;
    throw new Error(`unexpected request: ${path}`);
  };

  let intervalId = 0;
  let clearedInterval = 0;
  const core = loadCore({
    timerApi: {
      setInterval(_fn, _ms) {
        intervalId += 1;
        return intervalId;
      },
      clearInterval(id) {
        clearedInterval = Number(id || 0);
      },
    },
  });

  const titleNode = { textContent: "" };
  const listeners = new Map();
  let text = "";
  const contentNode = {
    clientHeight: 100,
    scrollTop: 0,
    scrollHeight: 0,
    addEventListener(type, handler) {
      listeners.set(type, handler);
    },
    removeEventListener(type) {
      listeners.delete(type);
    },
    get textContent() {
      return text;
    },
    set textContent(value) {
      text = String(value || "");
      this.scrollHeight = text.length * 2;
    },
  };
  const dialogNode = {
    open: false,
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
  };

  const viewer = core.createRunLogViewer({
    api: fakeApi,
    titleNode,
    contentNode,
    dialogNode,
    tailLimit: 3,
    followLimit: 2,
    backfillLimit: 2,
  });

  await viewer.open(77, "Demo");
  assert.equal(dialogNode.open, true);
  assert.equal(titleNode.textContent, "Logs • Demo • run 77");
  assert.match(contentNode.textContent, /line-10\nline-11\nline-12\n$/);
  assert.match(calls[0], /\/api\/runs\/77\/logs\?/);
  assert.match(calls[0], /tail=true/);

  await viewer.pollFollow();
  assert.match(contentNode.textContent, /line-13\n$/);
  assert.match(calls[1], /after_log_id=12/);

  await viewer.loadOlder();
  assert.match(contentNode.textContent, /^line-09\nline-10\n/);
  assert.match(calls[2], /before_log_id=10/);

  const snapshot = viewer.getState();
  assert.equal(snapshot.activeRunId, 77);
  assert.equal(snapshot.nextAfterLogId, 13);
  assert.equal(snapshot.nextBeforeLogId, 9);
  assert.equal(snapshot.hasMoreBefore, false);

  viewer.close();
  assert.equal(dialogNode.open, false);
  assert.equal(clearedInterval > 0, true);
  assert.equal(viewer.getState().activeRunId, null);
});
