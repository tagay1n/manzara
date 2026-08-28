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
        global: { active_tasks: 1, stop_all_state: stopAllState },
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

test("document cleanup page bootstraps from its snapshot cursor and queue API", async () => {
  const harness = createHarness({
    source: LIBRARY_DOCUMENT_CLEANUP_SOURCE,
    ids: DOCUMENT_CLEANUP_PAGE_IDS,
    selectors: ["[data-cleanup-mode]"],
    locationPathname: "/library/document-cleanup",
    apiResolver(path) {
      if (path === "/api/library/document-cleanup") {
        return {
          event_cursor: 73,
          stats: {
            active_plans: 2,
            pending_reviews: 1,
            failed_plans: 0,
            completed_plans: 5,
          },
        };
      }
      if (path === "/api/library/document-cleanup/queue?limit=200") {
        return {
          items: [
            {
              cleanup_id: 9,
              action: "move",
              reason: "non_tatar",
              source_path: "/books/a.pdf",
              status: "planned",
            },
          ],
        };
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();

  assert.match(harness.elements.get("cleanup-stat-grid").innerHTML, /Active plans/);
  assert.match(harness.elements.get("cleanup-list").innerHTML, /non_tatar/);
  assert.equal(harness.sse.config.initialCursor, 73);
  assert.equal(
    harness.sse.config.eventTypes.includes("library.document_cleanup_changed"),
    true,
  );
});

test("gemini page confirms and overrides an active blackout", async () => {
  let overridden = false;
  const harness = createHarness({
    source: GEMINI_SOURCE,
    ids: GEMINI_PAGE_IDS,
    locationPathname: "/gemini",
    apiResolver(path, options = {}) {
      if (path === "/api/gemini/state") {
        return {
          event_cursor: 91,
          gemini: {
            summary: { accounts: 0, keys: 0, models_seen: 0, exhausted_rows: 0 },
            global: {
              cycle_label: "2026-08-12",
              reset_at_utc: "2026-08-12T07:00:00+00:00",
              blackout_active: !overridden,
              blackout_window_active: true,
              blackout_overridden: overridden,
              blackout_end_utc: "2026-08-12T08:00:00+00:00",
              blackout_override_until: overridden
                ? "2026-08-12T08:00:00+00:00"
                : null,
              pause_active: false,
            },
            accounts: [],
          },
        };
      }
      if (path === "/api/gemini/override-blackout" && options.method === "POST") {
        overridden = true;
        return { ok: true, blackout_override_until: "2026-08-12T08:00:00+00:00" };
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();

  assert.equal(harness.elements.get("override-blackout-btn").hidden, false);
  harness.elements.get("override-blackout-btn").dispatch("click");
  await harness.flush();
  await harness.timer.runAllTimeouts();
  await harness.flush();

  const call = harness.apiCalls.find(
    (entry) => entry.path === "/api/gemini/override-blackout",
  );
  assert.equal(call.options.method, "POST");
  assert.equal(harness.elements.get("override-blackout-btn").hidden, true);
  assert.match(harness.elements.get("gemini-stat-grid").innerHTML, /overridden until/i);
});

test("gemini page prioritizes model capacity and hides diagnostic errors", async () => {
  const harness = createHarness({
    source: GEMINI_SOURCE,
    ids: GEMINI_PAGE_IDS,
    locationPathname: "/gemini",
    apiResolver(path) {
      if (path !== "/api/gemini/state") throw new Error(`unexpected path: ${path}`);
      return {
        event_cursor: 92,
        gemini: {
          summary: { accounts: 1, keys: 4, models_seen: 2, exhausted_rows: 3 },
          global: {
            cycle_label: "2026-08-27",
            reset_at_utc: "2026-08-28T07:00:00+00:00",
            blackout_active: false,
            blackout_window_active: false,
            blackout_overridden: false,
            blackout_start_utc: "2026-08-28T06:00:00+00:00",
            pause_active: false,
          },
          model_usage: [
            {
              model_name: "gemini-a",
              total_keys: 4,
              available_keys: 1,
              exhausted_keys: 3,
              usage_percent: 75,
              attempts_cycle: 10,
              success_cycle: 7,
            },
            {
              model_name: "gemini-b",
              total_keys: 4,
              available_keys: 4,
              exhausted_keys: 0,
              usage_percent: 0,
              attempts_cycle: 0,
              success_cycle: 0,
            },
          ],
          accounts: [
            {
              account_id: "acc-a",
              key_count: 1,
              keys: [
                {
                  key_id: "acc-a:key-1",
                  masked_key: "KEYA...A001",
                  exhausted_models: ["gemini-a"],
                  models: [
                    {
                      model_name: "gemini-a",
                      exhausted: true,
                      last_error_text: "technical quota message that must stay hidden",
                    },
                  ],
                },
              ],
            },
          ],
        },
      };
    },
  });
  await harness.flush();

  const modelHtml = harness.elements.get("gemini-model-usage").innerHTML;
  const accountsHtml = harness.elements.get("gemini-accounts").innerHTML;
  assert.match(modelHtml, /gemini-a/);
  assert.match(modelHtml, /75%/);
  assert.match(modelHtml, /3 of 4 keys exhausted/);
  assert.match(modelHtml, /gemini-b/);
  assert.doesNotMatch(accountsHtml, /technical quota message/);
  assert.doesNotMatch(accountsHtml, /cooldown:/);
  assert.doesNotMatch(accountsHtml, /last used:/);
});
