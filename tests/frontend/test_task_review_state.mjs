import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const SOURCE = readFileSync(
  new URL("../../static/task-review-state.js", import.meta.url),
  "utf-8",
);

function createStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
    value(key) {
      return values.get(key) || null;
    },
  };
}

function loadReviewState(storage, now = Date.parse("2026-08-30T08:00:00Z")) {
  const context = { window: { localStorage: storage }, Date, JSON, Number };
  vm.runInNewContext(SOURCE, context);
  return context.window.ManzaraTaskReview.createStore({ storage, now: () => now });
}

test("first catalog visit highlights only terminal results from the last 24 hours", () => {
  const storage = createStorage();
  const review = loadReviewState(storage);
  const tasks = [
    { task_id: "recent-ok", run: { run_id: 11, status: "completed", finished_at: "2026-08-30T07:00:00Z" } },
    { task_id: "recent-fail", run: { run_id: 12, status: "failed", finished_at: "2026-08-29T12:00:00Z" } },
    { task_id: "old", run: { run_id: 13, status: "completed", finished_at: "2026-08-28T07:00:00Z" } },
    { task_id: "stopped", run: { run_id: 14, status: "stopped", finished_at: "2026-08-30T07:00:00Z" } },
    { task_id: "running", run: { run_id: 15, status: "running", finished_at: null } },
  ];

  review.syncCatalog(tasks);

  assert.equal(review.isUnread(tasks[0]), true);
  assert.equal(review.isUnread(tasks[1]), true);
  assert.equal(review.isUnread(tasks[2]), false);
  assert.equal(review.isUnread(tasks[3]), false);
  assert.equal(review.isUnread(tasks[4]), false);
  const persisted = JSON.parse(storage.value("manzara.task-review.v1"));
  assert.equal(persisted.catalog_initialized, true);
  assert.equal(persisted.opened.old, 13);
});

test("opening a result persists it while a newer terminal run becomes unread", () => {
  const storage = createStorage({
    "manzara.task-review.v1": JSON.stringify({
      version: 1,
      catalog_initialized: true,
      opened: {},
    }),
  });
  const review = loadReviewState(storage);
  const completed = { task_id: "task", run: { run_id: 21, status: "completed" } };

  assert.equal(review.isUnread(completed), true);
  review.markOpened("task", 21);
  assert.equal(review.isUnread(completed), false);
  assert.equal(
    review.isUnread({ task_id: "task", run: { run_id: 22, status: "failed" } }),
    true,
  );
});

test("corrupt or unavailable browser storage falls back to safe in-memory state", () => {
  const corrupt = createStorage({ "manzara.task-review.v1": "not-json" });
  const review = loadReviewState(corrupt);
  review.syncCatalog([]);
  assert.equal(JSON.parse(corrupt.value("manzara.task-review.v1")).version, 1);

  const unavailable = {
    getItem() { throw new Error("blocked"); },
    setItem() { throw new Error("blocked"); },
  };
  const fallback = loadReviewState(unavailable);
  const task = { task_id: "task", run: { run_id: 31, status: "failed" } };
  fallback.markOpened("task", 31);
  assert.equal(fallback.isUnread(task), false);
});
