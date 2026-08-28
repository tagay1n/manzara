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
  assert.match(
    harness.elements.get("normalization-status").textContent,
    /classifications unavailable/,
  );
});

function createClassificationsResolver({ malicious = false } = {}) {
  return (path, options = {}) => {
    if (
      path === "/api/library/classifications/merge"
      && String(options?.method || "GET").toUpperCase() === "POST"
    ) {
      return {
        available: true,
        source_classification_id: 11,
        target_classification_id: 10,
        moved_docs_count: 4,
        schema_org_updated_count: 4,
        source_deleted: true,
      };
    }
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
  summary = null,
} = {}) {
  return (path) => {
    if (path === "/api/library/publishers") {
      return {
        global: { active_tasks: 0, stop_all_state: "disabled" },
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

test("library classifications merge action posts merge request", async () => {
  const harness = createHarness({
    source: LIBRARY_CLASSIFICATIONS_SOURCE,
    ids: CLASSIFICATIONS_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver: createClassificationsResolver(),
  });
  await harness.flush();

  harness.elements.get("merge-root").dispatch("click", {
    target: {
      closest(selector) {
        if (selector !== ".merge-execute-btn") return null;
        return {
          dataset: {
            sourceId: "11",
            targetId: "10",
          },
        };
      },
    },
  });
  await harness.flush();

  const mergeCall = harness.apiCalls.find((entry) => entry.path === "/api/library/classifications/merge");
  assert.ok(mergeCall);
  assert.equal(String(mergeCall.options?.method || "").toUpperCase(), "POST");
  const body = JSON.parse(String(mergeCall.options?.body || "{}"));
  assert.equal(body.source_classification_id, 11);
  assert.equal(body.target_classification_id, 10);
});

test("library classifications duplicates action posts merge request", async () => {
  const harness = createHarness({
    source: LIBRARY_CLASSIFICATIONS_SOURCE,
    ids: CLASSIFICATIONS_PAGE_IDS,
    selectors: [".classification-tabs"],
    apiResolver(path, options = {}) {
      if (
        path === "/api/library/classifications/merge"
        && String(options?.method || "GET").toUpperCase() === "POST"
      ) {
        return {
          available: true,
          source_classification_id: 2,
          target_classification_id: 1,
          moved_docs_count: 5,
          schema_org_updated_count: 5,
          source_deleted: true,
        };
      }
      if (path.startsWith("/api/library/classifications?")) {
        return {
          available: true,
          page: 1,
          total_pages: 1,
          total: 1,
          items: [
            {
              classification_id: 1,
              ddc: "891.7",
              path: "Language / Tatar",
              usage_count: 8,
              status: "active",
              created_by: "seed",
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
              path: "Language / Tatar",
              issue: "duplicate_path",
              total_usage: 12,
              distinct_ddc_count: 1,
              items: [
                { classification_id: 1, ddc: "891.7", usage_count: 8 },
                { classification_id: 2, ddc: "891.7", usage_count: 4 },
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
            affected_classifications: 0,
            estimated_reassigned_documents: 0,
            merge_group_candidates: 0,
          },
          merge_groups: [],
          affected_preview: [],
        };
      }
      if (path.startsWith("/api/library/classifications/merge-candidates?")) {
        return {
          available: true,
          summary: { candidate_count: 0, rows_scanned: 2, min_score: 0.8 },
          candidates: [],
        };
      }
      if (path === "/api/library") {
        return {
          global: { active_tasks: 0, stop_all_state: "disabled" },
        };
      }
      throw new Error(`unexpected path: ${path}`);
    },
  });
  await harness.flush();

  harness.elements.get("duplicates-root").dispatch("click", {
    target: {
      closest(selector) {
        if (selector !== ".duplicate-merge-btn") return null;
        return {
          dataset: {
            sourceId: "2",
            targetId: "1",
          },
        };
      },
    },
  });
  await harness.flush();

  const mergeCall = harness.apiCalls.find((entry) => entry.path === "/api/library/classifications/merge");
  assert.ok(mergeCall);
  assert.equal(String(mergeCall.options?.method || "").toUpperCase(), "POST");
  const body = JSON.parse(String(mergeCall.options?.body || "{}"));
  assert.equal(body.source_classification_id, 2);
  assert.equal(body.target_classification_id, 1);
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
    locationPathname: "/library/publishers",
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
