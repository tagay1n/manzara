import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const CORE_SOURCE = readFileSync(new URL("../../static/core.js", import.meta.url), "utf-8");

function loadCore({ fetchImpl, EventSourceImpl, timerApi } = {}) {
  const sandbox = {
    window: {},
    Intl,
    Date,
    JSON,
    Number,
    encodeURIComponent,
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
