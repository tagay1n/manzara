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
});

function createClassificationsResolver({ malicious = false, tree = [], documents = [] } = {}) {
  return (path, options = {}) => {
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
        revision: "revision-1",
        tree,
        distribution: [],
      };
    }
    if (path.startsWith("/api/library/classifications/documents?")) return { available: true, items: documents, total: documents.length, has_more: false };
    if (path.startsWith("/api/library/documents/") && path.endsWith("/cache")) return { available: true, open_url: "/api/library/documents/abc/local/book.pdf" };
    if (path === "/api/library/classifications/change-set/preview") return { available: true, change_set_hash: "hash-1", summary: { path_changes: 1, classification_merges: 0, affected_documents: 5 } };
    if (path === "/api/library/classifications/change-set/apply") return { available: true, applied: true };
    if (path === "/api/library") {
      return {
        global: { active_tasks: 0, stop_all_state: "disabled" },
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
        global: { active_tasks: 0, stop_all_state: "disabled" },
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
  groupCalls = [],
} = {}) {
  return (path, options = {}) => {
    if (path === "/api/library/normalization/publisher") {
      return {
        global: { active_tasks: 0, stop_all_state: "disabled" },
        dashboard: {
          available: true,
          config_source: "test",
          stats: {
            total_aliases: 2,
            canonicals: 1,
            linked: 0,
            unreviewed: 1,
            suggested: 1,
            coverage_pct: 0,
          },
        },
      };
    }
    if (path.startsWith("/api/library/normalization/publisher/queue?")) {
      return {
        available: true,
        page: 1,
        total_pages: 1,
        total: 2,
        items: [
          {
            raw_name: "Таткнигоиздат", normalized_name: "таткнигоиздат",
            script_label: "cyrillic", docs_count: 4, mentions_count: 4,
            queue_status: "unreviewed", canonical_id: null, canonical_name: null, suggestion: null,
          },
          {
            raw_name: "Tatknigoizdat", normalized_name: "tatknigoizdat",
            script_label: "latin", docs_count: 2, mentions_count: 2,
            queue_status: "suggested", canonical_id: 1, canonical_name: "Tatar Book Publisher",
            suggestion: { suggestion_id: 8, kind: "link", target_canonical_id: 1, confidence: 0.91 },
          },
        ],
      };
    }
    if (path.startsWith("/api/library/normalization/publisher/canonicals?")) {
      return { available: true, items: [{ canonical_id: 1, display_name: "Tatar Book Publisher", linked_aliases: 1 }] };
    }
    if (path === "/api/library/normalization/publisher/history?limit=100") return { available: true, items: [] };
    if (path === "/api/tasks/library.publisher_suggestions_refresh?limit=1") return { task: { task_id: "library.publisher_suggestions_refresh" }, runs: [] };
    if (path === "/api/library/normalization/publisher/groups") {
      groupCalls.push(JSON.parse(options.body || "{}"));
      return { canonical: { canonical_id: 2 }, aliases: [], event: { event_id: 1 } };
    }
    throw new Error(`unexpected path: ${path}`);
  };
}

function createCollectionsResolver({
  summary = null,
} = {}) {
  return (path, options = {}) => {
    if (path === "/api/library/collections") {
      return {
        global: { active_tasks: 0, stop_all_state: "disabled" },
        overview: {
          available: true,
          config_source: "test",
          stats: {
            approved_collections: 1,
            suggested_collections: 1,
            awaiting_validation: 2,
            items_linked: 14,
          },
          top_collections: [],
        },
      };
    }
    if (path.startsWith("/api/library/collection-proposals?")) {
      return {
        available: true,
        page: 1,
        total_pages: 1,
        total: 2,
        items: [
          {
            proposal_id: 11,
            proposal_type: "new_collection",
            title: "Collection One",
            status: "review_ready",
            confidence: 0.81,
            item_count: 3,
          },
        ],
      };
    }
    if (path === "/api/library/collection-proposals/11") {
      return {
        available: true,
        proposal: {
          proposal_id: 11,
          proposal_type: "new_collection",
          title: "Collection One",
          status: "review_ready",
          confidence: 0.81,
          rationale: "Recurring named newspaper",
        },
        items: [
          {
            md5: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            title: "Issue #1",
            publication_date: "1955-01-01",
            issue_number: "1",
            publishers: ["Publisher"],
            genres: ["Newspaper"],
            work_type: "NewsArticle",
            included: true,
            verdict: "belongs",
            confidence: 0.96,
            rationale: "Matching title and issue evidence",
            model: "gemini-3-flash-preview",
            selected_by_default: true,
          },
        ],
      };
    }
    if (path === "/api/library/collection-proposals/11/decision" && options.method === "POST") {
      return { ok: true, proposal_id: 11 };
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
    apiResolver: createClassificationsResolver({ malicious: true, tree: [{
      name: '<img src=x onerror=alert(1)>', path: ['<img src=x onerror=alert(1)>'], usage_count: 2,
      classifications: [{ classification_id: 2, ddc: '<script>alert(2)</script>', usage_count: 2 }], children: [],
    }] }),
  });
  await harness.flush();

  const tableHtml = harness.elements.get("classification-table-body").innerHTML;
  const combined = `${tableHtml}\n${harness.elements.get("tree-root").innerHTML}`;

  assert.equal(combined.includes("<img"), false);
  assert.equal(combined.includes("<script"), false);
  assert.doesNotMatch(combined, /href="[^"]*"\s+onclick=/);
  assert.match(combined, /&lt;img/);
  assert.match(combined, /&lt;script/);
});

test("library classifications hierarchy renders collapsible branches and toggles them", async () => {
  const harness = createHarness({
    source: LIBRARY_CLASSIFICATIONS_SOURCE,
    ids: CLASSIFICATIONS_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver: createClassificationsResolver({
      tree: [
        {
          name: "Literature",
          path: ["Literature"],
          usage_count: 12,
          classifications: [],
          children: [{ name: "Tatar literature", path: ["Literature", "Tatar literature"], usage_count: 8, classifications: [{ classification_id: 1, ddc: "891.7", usage_count: 8 }], children: [] }],
        },
      ],
    }),
  });
  await harness.flush();

  const treeRoot = harness.elements.get("tree-root");
  assert.match(treeRoot.innerHTML, /class="tree-toggle"/);
  assert.match(treeRoot.innerHTML, /aria-expanded="true"/);
  assert.match(treeRoot.innerHTML, /Tatar literature/);

  const toggle = {
    dataset: { treePath: '["Literature"]' },
  };
  treeRoot.dispatch("click", {
    target: {
      closest(selector) {
        return selector === ".tree-toggle" ? toggle : null;
      },
    },
  });

  assert.doesNotMatch(treeRoot.innerHTML, /Tatar literature/);
});

test("library classifications stages a subtree rename and supports undo", async () => {
  const harness = createHarness({
    source: LIBRARY_CLASSIFICATIONS_SOURCE,
    ids: CLASSIFICATIONS_PAGE_IDS,
    selectors: [".classification-tabs"],
    promptResult: "Books",
    apiResolver: createClassificationsResolver({ tree: [{ name: "Literature", path: ["Literature"], usage_count: 4, classifications: [], children: [{ name: "Tatar", path: ["Literature", "Tatar"], usage_count: 4, classifications: [{ classification_id: 11, ddc: "891.7", usage_count: 4 }], children: [] }] }] }),
  });
  await harness.flush();
  const treeRoot = harness.elements.get("tree-root");
  treeRoot.dispatch("click", {
    target: {
      closest(selector) {
        if (selector === "[data-action]") return { dataset: { action: "add-above", path: '["Literature","Tatar"]' } };
        return null;
      },
    },
  });
  await harness.flush();
  assert.match(treeRoot.innerHTML, /Books/);
  assert.equal(harness.elements.get("taxonomy-review").disabled, false);
  harness.elements.get("taxonomy-undo").dispatch("click");
  assert.doesNotMatch(treeRoot.innerHTML, /Books/);
});

test("library classifications reviews then atomically applies staged changes", async () => {
  const harness = createHarness({
    source: LIBRARY_CLASSIFICATIONS_SOURCE,
    ids: CLASSIFICATIONS_PAGE_IDS,
    selectors: [".classification-tabs"],
    promptResult: "Books",
    apiResolver: createClassificationsResolver({ tree: [{ name: "Literature", path: ["Literature"], usage_count: 4, classifications: [], children: [{ name: "Tatar", path: ["Literature", "Tatar"], usage_count: 4, classifications: [{ classification_id: 11, ddc: "891.7", usage_count: 4 }], children: [] }] }] }),
  });
  await harness.flush();
  harness.elements.get("tree-root").dispatch("click", { target: { closest(selector) { return selector === "[data-action]" ? { dataset: { action: "add-above", path: '["Literature","Tatar"]' } } : null; } } });
  await harness.flush();
  harness.elements.get("taxonomy-review").dispatch("click");
  await harness.flush();
  const preview = harness.apiCalls.find((entry) => entry.path.endsWith("/change-set/preview"));
  const apply = harness.apiCalls.find((entry) => entry.path.endsWith("/change-set/apply"));
  assert.ok(preview); assert.ok(apply);
  const body = JSON.parse(apply.options.body);
  assert.equal(body.confirmed, true); assert.equal(body.change_set_hash, "hash-1");
});

test("library classification leaves load documents and open through the local cache flow", async () => {
  const tree = [{ name: "Literature", path: ["Literature"], usage_count: 1, classifications: [], children: [{ name: "Tatar", path: ["Literature", "Tatar"], usage_count: 1, classifications: [{ classification_id: 7, ddc: "891.7", usage_count: 1 }], children: [] }] }];
  const harness = createHarness({
    source: LIBRARY_CLASSIFICATIONS_SOURCE,
    ids: CLASSIFICATIONS_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver: createClassificationsResolver({ tree, documents: [{ md5: "abc", title: "A real book", source_name: "book.pdf", language: "tt", mime_type: "application/pdf", page_count: 42 }] }),
  });
  await harness.flush();
  const root = harness.elements.get("tree-root");
  root.dispatch("click", { target: { closest(selector) { return selector === ".document-load-btn, .document-more-btn" ? { dataset: { path: '["Literature","Tatar"]' } } : null; } } });
  await harness.flush();
  assert.match(root.innerHTML, /A real book/);
  assert.match(root.innerHTML, /42 pages/);
  root.dispatch("click", { target: { closest(selector) { return selector === ".document-open-btn" ? { dataset: { md5: "abc" } } : null; } } });
  await harness.flush();
  assert.ok(harness.apiCalls.some((entry) => entry.path === "/api/library/documents/abc/cache" && entry.options.method === "POST"));
  assert.deepEqual(harness.openedWindows.at(-1), ["/api/library/documents/abc/local/book.pdf", "_blank", "noopener"]);
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
    locationPathname: "/library/publishers",
    ids: PUBLISHERS_PAGE_IDS,
    selectors: [".classification-tabs", ".publisher-row-select"],
    apiResolver(path) {
      if (path.startsWith("/api/library/normalization/publisher")) {
        throw new Error("publishers unavailable");
      }
      if (path.startsWith("/api/tasks/")) return { task: {}, runs: [] };
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
});

test("library publishers page groups selected aliases with a manual canonical name", async () => {
  const groupCalls = [];
  const harness = createHarness({
    source: LIBRARY_PUBLISHERS_SOURCE,
    locationPathname: "/library/publishers",
    ids: PUBLISHERS_PAGE_IDS,
    selectors: [".classification-tabs", ".publisher-row-select"],
    promptResult: "Татарстан китап нәшрияты",
    confirmResult: true,
    apiResolver: createPublishersResolver({ groupCalls }),
  });
  await harness.flush();

  harness.elements.get("publisher-table-body").dispatch("change", { target: { closest: () => ({ checked: true, dataset: { raw: encodeURIComponent("Таткнигоиздат") } }) } });
  harness.elements.get("filter-search").value = "Tat";
  harness.elements.get("filter-apply").dispatch("click");
  await harness.flush();
  assert.equal(harness.elements.get("publisher-selection-count").textContent, "1 name selected");
  harness.elements.get("publisher-create-group").dispatch("click");
  await harness.flush();

  assert.equal(groupCalls.length, 1);
  assert.equal(groupCalls[0].display_name, "Татарстан китап нәшрияты");
  assert.deepEqual(groupCalls[0].raw_names, ["Таткнигоиздат"]);
  assert.equal(harness.elements.get("publisher-selection-bar").hidden, true);
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
    harness.elements.get("collections-list-status").textContent,
    /collections unavailable/,
  );
  assert.match(harness.elements.get("collections-list-root").innerHTML, /collections unavailable/);
});

test("library collections page renders each proposal once", async () => {
  const harness = createHarness({
    source: LIBRARY_COLLECTIONS_SOURCE,
    ids: COLLECTIONS_PAGE_IDS,
    apiResolver: createCollectionsResolver(),
  });
  await harness.flush();

  const html = harness.elements.get("collections-list-root").innerHTML;
  assert.equal((html.match(/Collection One/g) || []).length, 1);
  assert.ok(harness.apiCalls.some((call) => call.path.startsWith("/api/library/collection-proposals?")));
});

test("library collections list expands proposal evidence with item selection", async () => {
  const harness = createHarness({
    source: LIBRARY_COLLECTIONS_SOURCE,
    ids: COLLECTIONS_PAGE_IDS,
    apiResolver: createCollectionsResolver(),
  });
  await harness.flush();

  const listRoot = harness.elements.get("collections-list-root");
  assert.match(listRoot.innerHTML, /collection-queue-trigger/);
  assert.match(listRoot.innerHTML, /aria-expanded="false"/);
  assert.equal((listRoot.innerHTML.match(/>Reject</g) || []).length, 1);
  assert.doesNotMatch(listRoot.innerHTML, />Approve</);

  listRoot.dispatch("click", {
    target: {
      closest(selector) {
        if (selector !== "[data-proposal-toggle]") return null;
        return { dataset: { proposalToggle: "11" } };
      },
    },
  });
  await harness.flush();

  assert.equal(
    harness.apiCalls.some(
      (call) => call.path === "/api/library/collection-proposals/11",
    ),
    true,
  );
  assert.match(listRoot.innerHTML, /aria-expanded="true"/);
  assert.match(listRoot.innerHTML, /Issue #1/);
  assert.match(
    listRoot.innerHTML,
    /\/api\/library\/documents\/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\/open/,
  );
  assert.match(listRoot.innerHTML, /target="_blank"/);
  assert.match(listRoot.innerHTML, /Review proposal only/);
  assert.match(listRoot.innerHTML, /Recurring named newspaper/);
  assert.match(listRoot.innerHTML, /Matching title and issue evidence/);
  assert.match(listRoot.innerHTML, /Approve selected/);
  assert.equal((listRoot.innerHTML.match(/>Reject</g) || []).length, 1);
  assert.match(listRoot.innerHTML, /type="checkbox"/);
});

test("library collections approval posts selected proposal decision", async () => {
  const harness = createHarness({
    source: LIBRARY_COLLECTIONS_SOURCE,
    ids: COLLECTIONS_PAGE_IDS,
    apiResolver: createCollectionsResolver(),
  });
  await harness.flush();

  harness.elements.get("collections-list-root").dispatch("click", {
    target: {
      closest(selector) {
        if (selector !== "[data-proposal-approve]") return null;
        return { dataset: { proposalApprove: "11" } };
      },
    },
  });
  await harness.flush();

  const decisionCall = harness.apiCalls.find(
    (call) => call.path === "/api/library/collection-proposals/11/decision" && call.options.method === "POST",
  );
  assert.ok(decisionCall);
  assert.equal(JSON.parse(decisionCall.options.body).decision, "approve");
  assert.equal(
    harness.apiCalls.some((call) => call.path.includes("collection_apply")),
    false,
  );
});

test("ISBN review opens a ready document and restores focus to the review tab", async () => {
  const md5 = "a".repeat(32);
  const openUrl = `/api/library/documents/${md5}/local`;
  const harness = createHarness({
    source: LIBRARY_DOCUMENT_CLEANUP_SOURCE,
    ids: DOCUMENT_CLEANUP_PAGE_IDS,
    apiResolver(path, options = {}) {
      if (path === "/api/library/document-cleanup") {
        return { event_cursor: 0 };
      }
      if (path === "/api/library/document-cleanup/isbn-reviews?status=pending&limit=500") {
        return {
          items: [{
            review_id: 7,
            isbn: "9780306406157",
            candidates_json: [{ md5, title: "Book", mime_type: "application/pdf" }],
          }],
        };
      }
      if (path === "/api/library/document-cleanup/isbn-reviews?status=decided&limit=20") {
        return { items: [] };
      }
      if (
        path === `/api/library/documents/${md5}/cache`
        && String(options.method || "GET").toUpperCase() === "POST"
      ) {
        return { open_url: openUrl };
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();

  harness.elements.get("cleanup-list").dispatch("click", {
    preventDefault() {},
    stopPropagation() {},
    target: {
      closest(selector) {
        if (selector !== "[data-document-md5]") return null;
        return { dataset: { documentMd5: md5 } };
      },
    },
  });
  await harness.flush();
  await harness.timer.runAllTimeouts();

  assert.deepEqual(harness.openedWindows, [[openUrl, "_blank", "noopener"]]);
  assert.equal(harness.windowFocuses.length, 2);
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
          global: { active_tasks: 0, stop_all_state: "disabled" },
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
