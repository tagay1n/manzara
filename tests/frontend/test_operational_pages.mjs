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

test("dashboard page renders empty state for panels and runs", async () => {
  const payload = {
    global: {
      active_tasks: 0,
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
  assert.equal(harness.elements.get("global-status").textContent, "Tasks: 0");
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
      "library-preview-status",
      "library-preview-grid",
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
  assert.match(harness.elements.get("library-preview-grid").innerHTML, /Error: library unavailable/);
});

test("library page renders preview coverage and applies live run progress without reload", async () => {
  const payload = {
    event_cursor: 71,
    global: { active_tasks: 1, stop_all_state: "normal" },
    dataset: {
      available: true,
      config_source: "config.yaml",
      stats: { applicable_docs: 25 },
      top_classifications: [],
      preview_stats: {
        recipe_version: "pdf-three-page-webp-v1",
        eligible: 19,
        ready: 7,
        pending: 10,
        partial: 1,
        failed: 1,
        generated_preview_pages: 18,
        generated_image_objects: 36,
      },
    },
    last_eval_run: null,
  };
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
      "library-preview-status",
      "library-preview-grid",
    ],
    apiResolver(path) {
      if (path === "/api/library") return JSON.parse(JSON.stringify(payload));
      throw new Error(`unexpected path: ${path}`);
    },
  });

  await harness.flush();
  assert.match(harness.elements.get("library-preview-grid").innerHTML, /Ready/);
  assert.match(harness.elements.get("library-preview-grid").innerHTML, />7</);
  const before = harness.apiCalls.filter((call) => call.path === "/api/library").length;

  harness.sse.config.onEvent({
    type: "task.progress",
    task_id: "library.generate_book_previews",
    panel_id: "library",
    payload: {
      progress: {
        current: 3,
        total: 10,
        ready: 2,
        partial: 1,
        failed: 0,
        uploaded_objects: 8,
        reused_objects: 4,
      },
    },
  }, { lastEventId: "72" });
  await harness.flush();

  const after = harness.apiCalls.filter((call) => call.path === "/api/library").length;
  assert.equal(after, before);
  assert.match(harness.elements.get("library-preview-status").textContent, /3 \/ 10/);
  assert.match(harness.elements.get("library-preview-status").textContent, /2 ready/);
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

test("database page refreshes after a Backup catalog task finishes", async () => {
  const payload = {
    event_cursor: 80,
    global: { active_tasks: 0, stop_all_state: "disabled" },
    database_state: {
      available: false,
      error: "metrics unavailable",
      captured_at: "2026-08-12T12:00:00Z",
      backup: {},
    },
  };
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
      if (path === "/api/database/state") return JSON.parse(JSON.stringify(payload));
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();
  const before = harness.apiCalls.length;

  harness.sse.config.onEvent({
    type: "task.completed",
    task_id: "maintenance.pgbackrest_backup_full",
    panel_id: "backup",
    payload: { status: "completed" },
  }, { lastEventId: "81" });
  await harness.timer.runAllTimeouts();
  await harness.flush();

  assert.equal(harness.apiCalls.length, before + 1);
});
