const state = {
  tablePayload: null,
  insightsPayload: null,
  normalizationPayload: null,
  mergePayload: null,
  globalPayload: null,
  viewState: "loading",
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  soundNotifier: null,
  page: 1,
  pageSize: 25,
  activeTab: "table",
  mergeActionKey: "",
  confirmPendingResolve: null,
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

const TAB_IDS = [
  "table",
  "tree",
  "distribution",
  "normalization",
  "merge",
  "duplicates",
  "unclassified",
];

const tabController = window.ManzaraCore.createTabController({
  tabs: TAB_IDS,
  getActiveTab: () => state.activeTab,
  setActiveTab: (tab) => {
    state.activeTab = tab;
  },
});

async function api(path, options = {}) {
  return window.ManzaraCore.api(path, options);
}

function escapeHtml(value) {
  return window.ManzaraCore.escapeHtml(value);
}

function encodePathSegment(value) {
  return encodeURIComponent(String(value ?? ""));
}

function toInt(value, fallback = 0) {
  const num = Number(value);
  if (!Number.isFinite(num)) return fallback;
  return Math.trunc(num);
}

function toId(value) {
  const num = toInt(value, 0);
  return num > 0 ? num : null;
}

function formatDateTime(value) {
  return window.ManzaraCore.formatDateTime(value);
}

function initSoundNotifier() {
  const createNotifier = window.ManzaraSound?.createNotifier;
  if (typeof createNotifier !== "function") return;
  state.soundNotifier = createNotifier({ repeatGapMs: 2000 });
}

function maybePlayTaskNotification(eventPayload, lastEventId = "") {
  state.soundNotifier?.handleEvent(eventPayload, lastEventId);
}

function teardownSoundNotifier() {
  if (state.soundNotifier && typeof state.soundNotifier.teardown === "function") {
    state.soundNotifier.teardown();
  }
  state.soundNotifier = null;
}

function renderGlobalState(payload) {
  const active = payload.global.active_tasks || 0;
  const activeWorkflows = payload.global.active_workflows || 0;
  document.getElementById("global-status").textContent = window.ManzaraCore.formatGlobalStatus(
    active,
    activeWorkflows
  );
  const stopBtn = document.getElementById("stop-all-btn");
  window.ManzaraCore.applyStopAllButton(stopBtn, payload.global.stop_all_state);
}

function currentFilters() {
  return {
    search: document.getElementById("filter-search").value.trim(),
    ddcPrefix: document.getElementById("filter-ddc-prefix").value.trim(),
    minUsage: Number(document.getElementById("filter-min-usage").value || "0"),
    status: document.getElementById("filter-status").value,
    sort: document.getElementById("filter-sort").value,
  };
}

function tableUrl() {
  const filters = currentFilters();
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
    search: filters.search,
    ddc_prefix: filters.ddcPrefix,
    min_usage: String(Math.max(0, filters.minUsage || 0)),
    status: filters.status,
    sort: filters.sort,
  });
  return `/api/library/classifications?${params.toString()}`;
}

function normalizationUrl() {
  const dropSegments = document
    .getElementById("normalization-drop-segments")
    .value.trim();
  const limitRaw = Number(
    document.getElementById("normalization-limit").value || "120"
  );
  const limit = Math.max(10, Math.min(500, Number.isFinite(limitRaw) ? limitRaw : 120));
  const params = new URLSearchParams({
    drop_segments: dropSegments || "Turkic literature",
    limit: String(limit),
  });
  return `/api/library/classifications/normalization-preview?${params.toString()}`;
}

function mergeCandidatesUrl() {
  const minScoreRaw = Number(document.getElementById("merge-min-score").value || "0.78");
  const limitRaw = Number(document.getElementById("merge-limit").value || "80");
  const minScore = Math.max(0, Math.min(1, Number.isFinite(minScoreRaw) ? minScoreRaw : 0.78));
  const limit = Math.max(10, Math.min(300, Number.isFinite(limitRaw) ? limitRaw : 80));
  const params = new URLSearchParams({
    min_score: String(minScore),
    limit: String(limit),
  });
  return `/api/library/classifications/merge-candidates?${params.toString()}`;
}

function classificationMergeActionKey(sourceId, targetId) {
  return `${String(sourceId || "")}->${String(targetId || "")}`;
}

function requestConfirmation(options = {}) {
  return window.ManzaraUI.confirm({
    title: String(options.title || "Confirm"),
    message: String(options.message || "Are you sure?"),
    acceptLabel: String(options.acceptLabel || "Confirm"),
    destructive: Boolean(options.destructive),
  });
}

function renderTable(payload) {
  state.tablePayload = payload;
  const statusNode = document.getElementById("classification-table-status");
  if (!payload.available) {
    statusNode.textContent = `Table unavailable: ${payload.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
    document.getElementById("classification-table-body").innerHTML = "";
    return;
  }
  statusNode.classList.remove("library-status-error");
  statusNode.textContent = `Loaded ${payload.items.length} rows from ${payload.total} total`;

  document.getElementById("classification-table-body").innerHTML = (payload.items || [])
    .map((item) => {
      const classificationId = toId(item.classification_id);
      const href = classificationId ? `/library/classifications/${encodePathSegment(classificationId)}` : "#";
      return `
      <tr>
        <td>${escapeHtml(String(classificationId ?? "-"))}</td>
        <td><a href="${href}" class="run-task-link">${escapeHtml(item.ddc || "-")}</a></td>
        <td title="${escapeHtml(item.path || "-")}">${escapeHtml(item.path || "-")}</td>
        <td>${escapeHtml(String(toInt(item.usage_count, 0)))}</td>
        <td>${escapeHtml(item.status || "-")}</td>
        <td>${escapeHtml(item.created_by || "-")}</td>
        <td>${escapeHtml(formatDateTime(item.created_at))}</td>
      </tr>
    `;
    })
    .join("");

  document.getElementById("page-label").textContent = `Page ${payload.page} / ${payload.total_pages}`;
  document.getElementById("page-prev").disabled = payload.page <= 1;
  document.getElementById("page-next").disabled = payload.page >= payload.total_pages;
}

function applyActiveTab() {
  tabController.apply();
}

function switchTab(tab) {
  tabController.select(tab);
}

function renderTreeNodes(nodes, depth = 0) {
  if (!nodes || !nodes.length) {
    return depth === 0 ? '<div class="workflow-footnote">No hierarchy data.</div>' : "";
  }
  return `
    <ul class="tree-list ${depth === 0 ? "root" : ""}">
      ${nodes
        .map(
          (node) => `
        <li>
          <div class="tree-row">
            <span class="tree-name">${escapeHtml(node.name || "-")}</span>
            <span class="tree-count">${escapeHtml(String(node.usage_count || 0))}</span>
          </div>
          ${renderTreeNodes(node.children || [], depth + 1)}
        </li>
      `
        )
        .join("")}
    </ul>
  `;
}

function renderDistribution(items) {
  if (!items || !items.length) {
    return '<div class="workflow-footnote">No distribution data.</div>';
  }
  return items
    .map(
      (item) => `
      <div class="distribution-row">
        <div class="distribution-head">
          <span>${escapeHtml(item.bucket || "-")}</span>
          <span>${escapeHtml(String(item.usage_count || 0))} • ${escapeHtml(String(item.share_pct || 0))}%</span>
        </div>
        <div class="distribution-bar">
          <span style="width: ${Math.max(0, Math.min(100, Number(item.share_pct || 0)))}%"></span>
        </div>
      </div>
    `
    )
    .join("");
}

function renderDuplicates(items) {
  if (!items || !items.length) {
    return '<div class="workflow-footnote">No duplicates detected.</div>';
  }
  return items
    .map(
      (group) => `
      <div class="duplicate-card">
        <div class="duplicate-head">
          <span class="duplicate-path">${escapeHtml(group.path || "-")}</span>
          <span class="panel-pill">${escapeHtml(group.issue || "-")}</span>
        </div>
        <div class="workflow-footnote">Usage ${group.total_usage} • DDC variants ${group.distinct_ddc_count}</div>
        ${(() => {
          const entries = Array.isArray(group.items) ? group.items : [];
          if (!entries.length) {
            return '<div class="workflow-footnote">No classification rows.</div>';
          }
          const primary = entries[0] || {};
          const primaryId = toId(primary.classification_id);
          const primaryHref = primaryId
            ? `/library/classifications/${encodePathSegment(primaryId)}`
            : "#";
          const mergeRows = entries
            .slice(1)
            .map((item) => {
              const classificationId = toId(item.classification_id);
              const href = classificationId
                ? `/library/classifications/${encodePathSegment(classificationId)}`
                : "#";
              const actionEnabled = primaryId && classificationId;
              const actionKey = classificationMergeActionKey(classificationId, primaryId);
              const actionBusy = actionEnabled && state.mergeActionKey === actionKey;
              return `
              <div class="duplicate-merge-row">
                <a class="duplicate-item" href="${href}">
                  <span>#${escapeHtml(String(classificationId ?? "-"))} ${escapeHtml(item.ddc || "-")}</span>
                  <span>Usage ${escapeHtml(String(item.usage_count || 0))}</span>
                </a>
                <button
                  class="small-btn duplicate-merge-btn"
                  data-source-id="${escapeHtml(String(classificationId ?? ""))}"
                  data-target-id="${escapeHtml(String(primaryId ?? ""))}"
                  ${actionEnabled && !actionBusy ? "" : "disabled"}
                >${actionBusy ? "Merging..." : `Merge into #${escapeHtml(String(primaryId ?? "-"))}`}</button>
              </div>
            `;
            })
            .join("");
          return `
            <div class="duplicate-items">
              <a class="duplicate-item duplicate-primary" href="${primaryHref}">
                <span>Keep #${escapeHtml(String(primaryId ?? "-"))} ${escapeHtml(primary.ddc || "-")}</span>
                <span>Usage ${escapeHtml(String(primary.usage_count || 0))}</span>
              </a>
              ${mergeRows || '<div class="workflow-footnote">No merge actions for this group.</div>'}
            </div>
          `;
        })()}
      </div>
    `
    )
    .join("");
}

function renderUnclassified(queue) {
  if (!queue || !queue.items || !queue.items.length) {
    return `<div class="workflow-footnote">Queue is empty.</div>`;
  }
  const rows = queue.items
    .map(
      (item) => `
      <div class="run-row">
        <div>${escapeHtml(item.md5 || "-")} • ${escapeHtml(item.language || "-")}</div>
        <div title="${escapeHtml(item.ya_path || "-")}">${escapeHtml(item.mime_type || "-")}</div>
      </div>
    `
    )
    .join("");
  return `
    <div class="workflow-footnote">Total in queue: ${queue.total || 0}</div>
    ${rows}
  `;
}

function renderNormalization(payload) {
  state.normalizationPayload = payload;
  const statusNode = document.getElementById("normalization-status");
  const summaryNode = document.getElementById("normalization-summary");
  const groupsNode = document.getElementById("normalization-groups");
  const affectedNode = document.getElementById("normalization-affected");

  if (!payload.available) {
    statusNode.textContent = `Normalization unavailable: ${payload.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
    summaryNode.innerHTML = "";
    groupsNode.innerHTML = "";
    affectedNode.innerHTML = "";
    const normalizationBadge = document.getElementById("tab-badge-normalization");
    if (normalizationBadge) normalizationBadge.textContent = "0";
    return;
  }

  statusNode.classList.remove("library-status-error");
  statusNode.textContent = `Rules: ${payload.rules.drop_segments.join(", ")}`;
  summaryNode.innerHTML = `
    <div class="library-stat-card"><span class="library-stat-label">Rows Scanned</span><span class="library-stat-value">${payload.summary.total_rows_scanned || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Affected</span><span class="library-stat-value">${payload.summary.affected_classifications || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Estimated Reassignments</span><span class="library-stat-value">${payload.summary.estimated_reassigned_documents || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Merge Groups</span><span class="library-stat-value">${payload.summary.merge_group_candidates || 0}</span></div>
  `;

  groupsNode.innerHTML = `
    <h3 class="mini-head">Merge Groups After Normalization</h3>
    ${
      (payload.merge_groups || []).length
        ? (payload.merge_groups || [])
            .slice(0, 25)
            .map(
              (group) => `
            <div class="duplicate-card">
              <div class="duplicate-head">
                <span class="duplicate-path">${escapeHtml(group.normalized_path || "-")}</span>
                <span class="panel-pill">group ${group.group_size}</span>
              </div>
              <div class="workflow-footnote">Usage ${group.total_usage} • primary ${group.recommended_primary_classification_id}</div>
            </div>
          `
            )
            .join("")
        : '<div class="workflow-footnote">No merge groups detected.</div>'
    }
  `;

  affectedNode.innerHTML = `
    <h3 class="mini-head">Affected Classifications Preview</h3>
    ${
      (payload.affected_preview || []).length
        ? (payload.affected_preview || [])
            .slice(0, 30)
            .map(
              (item) => `
            <div class="duplicate-card">
              <div class="duplicate-head">
                <span class="duplicate-path">${escapeHtml(item.original_path || "-")}</span>
                <span class="panel-pill">#${escapeHtml(String(toId(item.classification_id) ?? "-"))}</span>
              </div>
              <div class="workflow-footnote">→ ${escapeHtml(item.normalized_path || "-")} • usage ${escapeHtml(String(item.usage_count ?? 0))}</div>
            </div>
          `
            )
            .join("")
        : '<div class="workflow-footnote">No affected classifications.</div>'
    }
  `;

  const normalizationBadge = document.getElementById("tab-badge-normalization");
  if (normalizationBadge) {
    normalizationBadge.textContent = String(payload.summary.affected_classifications || 0);
  }
}

function renderMergeCandidates(payload) {
  state.mergePayload = payload;
  const statusNode = document.getElementById("merge-status");
  const summaryNode = document.getElementById("merge-summary");
  const rootNode = document.getElementById("merge-root");

  if (!payload.available) {
    statusNode.textContent = `Merge candidates unavailable: ${payload.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
    summaryNode.innerHTML = "";
    rootNode.innerHTML = "";
    const mergeBadge = document.getElementById("tab-badge-merge");
    if (mergeBadge) mergeBadge.textContent = "0";
    return;
  }

  statusNode.classList.remove("library-status-error");
  statusNode.textContent = "Pick the classification to keep, then merge the duplicate into it.";
  summaryNode.innerHTML = `
    <div class="library-stat-card"><span class="library-stat-label">Candidates</span><span class="library-stat-value">${payload.summary.candidate_count || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Rows Scanned</span><span class="library-stat-value">${payload.summary.rows_scanned || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Min Score</span><span class="library-stat-value">${Number(payload.summary.min_score || 0).toFixed(2)}</span></div>
  `;
  rootNode.innerHTML = (payload.candidates || []).length
    ? (payload.candidates || [])
        .map((candidate) => {
          const primaryId = toId(candidate.primary.classification_id);
          const secondaryId = toId(candidate.secondary.classification_id);
          const recommendedPrimaryId = toId(candidate.recommended_primary_classification_id);
          const keepId = recommendedPrimaryId || primaryId || secondaryId;
          const mergeSourceId = keepId === primaryId ? secondaryId : primaryId;
          const actionEnabled = Number(mergeSourceId || 0) > 0 && Number(keepId || 0) > 0;
          const actionKey = classificationMergeActionKey(mergeSourceId, keepId);
          const actionBusy = actionEnabled && state.mergeActionKey === actionKey;
          const primaryHref = primaryId ? `/library/classifications/${encodePathSegment(primaryId)}` : "#";
          const secondaryHref = secondaryId
            ? `/library/classifications/${encodePathSegment(secondaryId)}`
            : "#";
          return `
        <div class="duplicate-card">
          <div class="duplicate-head">
            <span class="duplicate-path">Candidate ${escapeHtml(String(primaryId ?? "-"))} ↔ ${escapeHtml(String(secondaryId ?? "-"))}</span>
            <span class="panel-pill">${escapeHtml(candidate.issue || "-")}</span>
          </div>
          <div class="workflow-footnote">Score ${escapeHtml(String(candidate.score ?? 0))} • Impact ${escapeHtml(String(candidate.impact ?? 0))}</div>
          <div class="merge-candidate-grid">
            <a class="duplicate-item merge-candidate-col" href="${primaryHref}">
              <span class="merge-candidate-label">${primaryId === keepId ? "Keep" : "Merge into keep"}</span>
              <span>#${escapeHtml(String(primaryId ?? "-"))} ${escapeHtml(candidate.primary.ddc || "-")}</span>
              <span>${escapeHtml(candidate.primary.path || "-")}</span>
              <span>Usage ${escapeHtml(String(candidate.primary.usage_count ?? 0))}</span>
            </a>
            <a class="duplicate-item merge-candidate-col" href="${secondaryHref}">
              <span class="merge-candidate-label">${secondaryId === keepId ? "Keep" : "Merge into keep"}</span>
              <span>#${escapeHtml(String(secondaryId ?? "-"))} ${escapeHtml(candidate.secondary.ddc || "-")}</span>
              <span>${escapeHtml(candidate.secondary.path || "-")}</span>
              <span>Usage ${escapeHtml(String(candidate.secondary.usage_count ?? 0))}</span>
            </a>
          </div>
          <div class="normalization-action-row">
            <button
              class="small-btn merge-execute-btn"
              data-source-id="${escapeHtml(String(mergeSourceId ?? ""))}"
              data-target-id="${escapeHtml(String(keepId ?? ""))}"
              ${actionEnabled && !actionBusy ? "" : "disabled"}
            >${actionBusy ? "Merging..." : "Merge"}</button>
            <span class="workflow-footnote">Action: merge #${escapeHtml(String(mergeSourceId ?? "-"))} into #${escapeHtml(String(keepId ?? "-"))}</span>
          </div>
        </div>
      `;
        })
        .join("")
    : '<div class="workflow-footnote">No merge candidates at this threshold.</div>';

  const mergeBadge = document.getElementById("tab-badge-merge");
  if (mergeBadge) {
    mergeBadge.textContent = String(payload.summary.candidate_count || 0);
  }
}

async function applyClassificationMerge(
  sourceClassificationId,
  targetClassificationId,
  options = {}
) {
  const statusNodeId = String(options.statusNodeId || "merge-status");
  const sourceId = toId(sourceClassificationId);
  const targetId = toId(targetClassificationId);
  if (!sourceId || !targetId || sourceId === targetId) return;
  const confirmed = await requestConfirmation({
    title: "Confirm merge",
    message: `Merge classification #${sourceId} into #${targetId}? This action is one-way.`,
    acceptLabel: "Merge",
    destructive: true,
  });
  if (!confirmed) return;

  state.mergeActionKey = classificationMergeActionKey(sourceId, targetId);
  if (state.mergePayload) renderMergeCandidates(state.mergePayload);
  try {
    const result = await api("/api/library/classifications/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_classification_id: sourceId,
        target_classification_id: targetId,
        reason: "manual_merge_from_candidates",
      }),
    });
    await Promise.all([refreshTable(), refreshInsights(), refreshMergeCandidates()]);
    const moved = toInt(result?.moved_docs_count, 0);
    const updated = toInt(result?.schema_org_updated_count, 0);
    const statusNode = document.getElementById(statusNodeId);
    if (statusNode) {
      statusNode.classList.remove("library-status-error");
      statusNode.textContent = `Merged #${sourceId} into #${targetId}. Moved docs ${moved}, schema updates ${updated}.`;
    }
  } catch (error) {
    const message = String(error?.message || error || "unknown error");
    const statusNode = document.getElementById(statusNodeId);
    if (statusNode) {
      statusNode.classList.add("library-status-error");
      statusNode.textContent = `Merge failed: ${message}`;
    }
    throw error;
  } finally {
    state.mergeActionKey = "";
    if (state.mergePayload) renderMergeCandidates(state.mergePayload);
  }
}

function renderInsights(payload) {
  state.insightsPayload = payload;
  const duplicatesStatusNode = document.getElementById("duplicates-status");
  if (!payload.available) {
    document.getElementById("tree-root").innerHTML = `<div class="workflow-footnote library-status-error">${escapeHtml(payload.error || "unknown error")}</div>`;
    document.getElementById("distribution-root").innerHTML = "";
    document.getElementById("duplicates-root").innerHTML = "";
    document.getElementById("unclassified-root").innerHTML = "";
    if (duplicatesStatusNode) {
      duplicatesStatusNode.textContent = `Duplicates unavailable: ${payload.error || "unknown error"}`;
      duplicatesStatusNode.classList.add("library-status-error");
    }
    const duplicatesBadge = document.getElementById("tab-badge-duplicates");
    if (duplicatesBadge) duplicatesBadge.textContent = "0";
    const unclassifiedBadge = document.getElementById("tab-badge-unclassified");
    if (unclassifiedBadge) unclassifiedBadge.textContent = "0";
    return;
  }
  if (duplicatesStatusNode) {
    const count = Array.isArray(payload.duplicates) ? payload.duplicates.length : 0;
    duplicatesStatusNode.textContent = `Detected ${count} duplicate/drift groups`;
    duplicatesStatusNode.classList.remove("library-status-error");
  }
  document.getElementById("tree-root").innerHTML = renderTreeNodes(payload.tree || []);
  document.getElementById("distribution-root").innerHTML = renderDistribution(payload.distribution || []);
  document.getElementById("duplicates-root").innerHTML = renderDuplicates(payload.duplicates || []);
  document.getElementById("unclassified-root").innerHTML = renderUnclassified(payload.unclassified_queue || {});
  const duplicatesBadge = document.getElementById("tab-badge-duplicates");
  if (duplicatesBadge) {
    duplicatesBadge.textContent = String((payload.duplicates || []).length);
  }
  const unclassifiedBadge = document.getElementById("tab-badge-unclassified");
  if (unclassifiedBadge) {
    unclassifiedBadge.textContent = String(
      Number(payload.unclassified_queue?.total || 0)
    );
  }
}

async function refreshTable() {
  const payload = await api(tableUrl());
  renderTable(payload);
}

async function refreshInsights() {
  const payload = await api("/api/library/classifications/insights");
  renderInsights(payload);
}

async function refreshNormalization() {
  const payload = await api(normalizationUrl());
  renderNormalization(payload);
}

async function refreshMergeCandidates() {
  const payload = await api(mergeCandidatesUrl());
  renderMergeCandidates(payload);
}

async function refreshGlobal() {
  const payload = await api("/api/library");
  state.globalPayload = payload;
  renderGlobalState(payload);
}

async function refreshAll() {
  await refreshAllWithState({});
}

function renderPageLoading() {
  viewState.set("loading");
  document.getElementById("classification-table-status").textContent = "Loading classifications...";
  document.getElementById("classification-table-status").classList.remove("library-status-error");
  document.getElementById("classification-table-body").innerHTML =
    '<tr><td colspan="7">Loading classifications...</td></tr>';
  document.getElementById("tree-root").innerHTML = '<div class="workflow-footnote">Loading hierarchy tree...</div>';
  document.getElementById("distribution-root").innerHTML =
    '<div class="workflow-footnote">Loading DDC distribution...</div>';
  document.getElementById("duplicates-root").innerHTML =
    '<div class="workflow-footnote">Loading duplicates and drift...</div>';
  const duplicatesStatusNode = document.getElementById("duplicates-status");
  if (duplicatesStatusNode) {
    duplicatesStatusNode.textContent = "Loading duplicates and drift...";
    duplicatesStatusNode.classList.remove("library-status-error");
  }
  document.getElementById("unclassified-root").innerHTML =
    '<div class="workflow-footnote">Loading unclassified queue...</div>';
  document.getElementById("normalization-status").textContent = "Loading normalization preview...";
  document.getElementById("normalization-status").classList.remove("library-status-error");
  document.getElementById("normalization-summary").innerHTML = "";
  document.getElementById("normalization-groups").innerHTML = "";
  document.getElementById("normalization-affected").innerHTML = "";
  document.getElementById("merge-status").textContent = "Loading merge candidates...";
  document.getElementById("merge-status").classList.remove("library-status-error");
  document.getElementById("merge-summary").innerHTML = "";
  document.getElementById("merge-root").innerHTML = "";
}

function renderPageError(error) {
  viewState.set("error");
  const message = String(error?.message || error || "Failed to load classifications.");
  const safe = escapeHtml(message);
  document.getElementById("classification-table-status").textContent =
    `Classifications unavailable: ${message}`;
  document.getElementById("classification-table-status").classList.add("library-status-error");
  document.getElementById("classification-table-body").innerHTML = "";
  document.getElementById("tree-root").innerHTML =
    `<div class="workflow-footnote library-status-error">${safe}</div>`;
  document.getElementById("distribution-root").innerHTML =
    `<div class="workflow-footnote library-status-error">${safe}</div>`;
  document.getElementById("duplicates-root").innerHTML =
    `<div class="workflow-footnote library-status-error">${safe}</div>`;
  const duplicatesStatusNode = document.getElementById("duplicates-status");
  if (duplicatesStatusNode) {
    duplicatesStatusNode.textContent = `Duplicates unavailable: ${message}`;
    duplicatesStatusNode.classList.add("library-status-error");
  }
  document.getElementById("unclassified-root").innerHTML =
    `<div class="workflow-footnote library-status-error">${safe}</div>`;
  document.getElementById("normalization-status").textContent =
    `Normalization unavailable: ${message}`;
  document.getElementById("normalization-status").classList.add("library-status-error");
  document.getElementById("normalization-summary").innerHTML = "";
  document.getElementById("normalization-groups").innerHTML = "";
  document.getElementById("normalization-affected").innerHTML = "";
  document.getElementById("merge-status").textContent = `Merge candidates unavailable: ${message}`;
  document.getElementById("merge-status").classList.add("library-status-error");
  document.getElementById("merge-summary").innerHTML = "";
  document.getElementById("merge-root").innerHTML = "";
  const duplicatesBadge = document.getElementById("tab-badge-duplicates");
  if (duplicatesBadge) duplicatesBadge.textContent = "0";
  const mergeBadge = document.getElementById("tab-badge-merge");
  if (mergeBadge) mergeBadge.textContent = "0";
  const normalizationBadge = document.getElementById("tab-badge-normalization");
  if (normalizationBadge) normalizationBadge.textContent = "0";
  const unclassifiedBadge = document.getElementById("tab-badge-unclassified");
  if (unclassifiedBadge) unclassifiedBadge.textContent = "0";
}

async function refreshAllWithState({ showLoading = false } = {}) {
  if (showLoading) {
    renderPageLoading();
  }
  try {
    await Promise.all([
      refreshTable(),
      refreshInsights(),
      refreshNormalization(),
      refreshMergeCandidates(),
      refreshGlobal(),
    ]);
    viewState.set("ready");
    applyActiveTab();
    lucide.createIcons();
  } catch (error) {
    renderPageError(error);
    throw error;
  }
}

function queueRefresh(delayMs = 250) {
  window.ManzaraCore.scheduleRefresh(state, refreshAll, delayMs);
}

async function stopAll() {
  const stopState = state.globalPayload?.global?.stop_all_state;
  if (stopState === "armed") {
    const confirmed = await requestConfirmation({
      title: "Force stop all tasks",
      message: "Force stop all running tasks immediately?",
      acceptLabel: "Force stop",
      destructive: true,
    });
    if (!confirmed) return;
  }
  await api("/api/system/stop-all", { method: "POST" });
  queueRefresh(0);
}

function setupEventStream() {
  state.eventStreamController?.stop();
  state.eventStreamController = window.ManzaraCore.createSseController({
    eventTypes: window.ManzaraCore.DEFAULT_EVENT_TYPES,
    getCursor: () => Number(state.eventCursor || 0),
    setCursor: (nextCursor) => {
      state.eventCursor = Number(nextCursor || 0);
    },
    onEvent: (payload, event) => {
      document.getElementById("last-event").textContent = window.ManzaraCore.formatEventBanner(payload);
      maybePlayTaskNotification(payload, event.lastEventId || "");
      const eventType = String(payload?.type || "");
      const taskFinished = ["task.artifact", "task.completed", "task.failed", "task.stopped"]
        .includes(eventType);
      if (
        eventType.startsWith("library.")
        || (String(payload?.panel_id || "") === "library" && taskFinished)
      ) {
        queueRefresh(150);
      }
    },
  });
  state.eventStreamController.start();
}

function attachUiHandlers() {
  document.getElementById("stop-all-btn").addEventListener("click", () => {
    stopAll().catch((error) => console.error(error));
  });

  document.getElementById("filter-apply").addEventListener("click", () => {
    state.page = 1;
    switchTab("table");
    queueRefresh(0);
  });

  document.getElementById("filter-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      state.page = 1;
      switchTab("table");
      queueRefresh(0);
    }
  });

  document.getElementById("page-prev").addEventListener("click", () => {
    const current = state.tablePayload?.page || 1;
    if (current <= 1) return;
    state.page = current - 1;
    queueRefresh(0);
  });

  document.getElementById("page-next").addEventListener("click", () => {
    const current = state.tablePayload?.page || 1;
    const total = state.tablePayload?.total_pages || 1;
    if (current >= total) return;
    state.page = current + 1;
    queueRefresh(0);
  });

  document.querySelector(".classification-tabs").addEventListener("click", (event) => {
    const button = event.target.closest(".classification-tab");
    if (!button) return;
    switchTab(String(button.dataset.tab || "table"));
  });

  document.getElementById("normalization-refresh").addEventListener("click", () => {
    switchTab("normalization");
    refreshNormalization().catch((error) => console.error(error));
  });

  document.getElementById("merge-refresh").addEventListener("click", () => {
    switchTab("merge");
    refreshMergeCandidates().catch((error) => console.error(error));
  });

  document.getElementById("merge-root").addEventListener("click", (event) => {
    const button = event.target.closest(".merge-execute-btn");
    if (!button) return;
    const sourceId = toId(button.dataset.sourceId);
    const targetId = toId(button.dataset.targetId);
    if (!sourceId || !targetId) return;
    applyClassificationMerge(sourceId, targetId).catch((error) => console.error(error));
  });

  document.getElementById("duplicates-root").addEventListener("click", (event) => {
    const button = event.target.closest(".duplicate-merge-btn");
    if (!button) return;
    const sourceId = toId(button.dataset.sourceId);
    const targetId = toId(button.dataset.targetId);
    if (!sourceId || !targetId) return;
    applyClassificationMerge(sourceId, targetId, { statusNodeId: "duplicates-status" }).catch(
      (error) => console.error(error)
    );
  });
}

async function bootstrap() {
  initSoundNotifier();
  window.addEventListener("beforeunload", () => {
    teardownSoundNotifier();
    if (state.eventStreamController) {
      state.eventStreamController.stop();
      state.eventStreamController = null;
    }

  });
  attachUiHandlers();
  await refreshAllWithState({ showLoading: true });
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  const message = String(error?.message || error || "Failed to load classifications.");
  const statusNode = document.getElementById("classification-table-status");
  if (statusNode) {
    statusNode.textContent = `Classifications unavailable: ${message}`;
    statusNode.classList.add("library-status-error");
  }
});
