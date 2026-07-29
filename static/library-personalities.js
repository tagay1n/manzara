const state = {
  overviewPayload: null,
  tablePayload: null,
  insightsPayload: null,
  globalPayload: null,
  viewState: "loading",
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  soundNotifier: null,
  page: 1,
  pageSize: 25,
  activeTab: "table",
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

const TAB_IDS = ["table", "scripts", "clusters", "queue"];

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

function setStatusMessage(node, text, options = {}) {
  window.ManzaraCore.setStatusMessage(node, text, options);
}

function renderRunRowMessage(text, options = {}) {
  return window.ManzaraCore.renderRunRowMessage(text, options);
}

function renderWorkflowFootnoteMessage(text, options = {}) {
  return window.ManzaraCore.renderWorkflowFootnoteMessage(text, options);
}

function renderLoadingTableRow(colSpan, text) {
  return window.ManzaraCore.renderLoadingTableRow(colSpan, text);
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
    scriptLabel: document.getElementById("filter-script").value,
    minDocs: Number(document.getElementById("filter-min-docs").value || "0"),
    sort: document.getElementById("filter-sort").value,
  };
}

function tableUrl() {
  const filters = currentFilters();
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
    search: filters.search,
    script_label: filters.scriptLabel,
    min_docs: String(Math.max(0, filters.minDocs || 0)),
    sort: filters.sort,
  });
  return `/api/library/personalities/table?${params.toString()}`;
}

function renderOverview(overview) {
  state.overviewPayload = overview;
  const statusNode = document.getElementById("personality-status");
  const stats = overview.stats || {};
  if (overview.available) {
    const source = overview.config_source ? ` • source: ${overview.config_source}` : "";
    setStatusMessage(statusNode, `Personality stats loaded${source}`, { error: false });
  } else {
    setStatusMessage(
      statusNode,
      `Personality dataset unavailable: ${overview.error || "unknown error"}`,
      { error: true }
    );
  }

  document.getElementById("personality-stat-grid").innerHTML = `
    <div class="library-stat-card"><span class="library-stat-label">Mentions</span><span class="library-stat-value">${stats.total_mentions || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Docs With Authors</span><span class="library-stat-value">${stats.docs_with_authors || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Raw Names</span><span class="library-stat-value">${stats.unique_raw_names || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Normalized Keys</span><span class="library-stat-value">${stats.unique_normalized_names || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Mixed Script</span><span class="library-stat-value">${stats.mixed_script_mentions || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Patronymic Form</span><span class="library-stat-value">${stats.patronymic_mentions || 0}</span></div>
  `;

  const topRows = (overview.top_personalities || [])
    .slice(0, 12)
    .map(
      (item) => `
      <div class="library-top-row">
        <div class="library-top-main">
          <span class="library-top-ddc">${escapeHtml(item.raw_name || "-")}</span>
          <span class="library-top-path">${escapeHtml(item.normalized_name || "-")} • ${escapeHtml(item.script_label || "other")}</span>
        </div>
        <span class="library-top-count">${escapeHtml(String(item.docs_count || 0))}</span>
      </div>
    `
    )
    .join("");
  document.getElementById("personality-top-list").innerHTML =
    topRows || '<div class="run-row">No personalities found yet.</div>';
}

function renderTable(payload) {
  state.tablePayload = payload;
  const statusNode = document.getElementById("personality-table-status");
  if (!payload.available) {
    setStatusMessage(statusNode, `Table unavailable: ${payload.error || "unknown error"}`, {
      error: true,
    });
    document.getElementById("personality-table-body").innerHTML = "";
    return;
  }
  setStatusMessage(statusNode, `Loaded ${payload.items.length} rows from ${payload.total} total`, {
    error: false,
  });

  document.getElementById("personality-table-body").innerHTML = (payload.items || [])
    .map(
      (item) => `
      <tr>
        <td>${escapeHtml(item.raw_name || "-")}</td>
        <td>${escapeHtml(item.normalized_name || "-")}</td>
        <td>${escapeHtml(item.script_label || "other")}</td>
        <td>${item.docs_count || 0}</td>
        <td>${item.mentions_count || 0}</td>
        <td>${item.patronymic_mentions || 0}</td>
      </tr>
    `
    )
    .join("");

  window.ManzaraCore.applyPaginationControls({
    labelNode: document.getElementById("page-label"),
    prevNode: document.getElementById("page-prev"),
    nextNode: document.getElementById("page-next"),
    page: payload.page,
    totalPages: payload.total_pages,
  });
}

function renderDistribution(items) {
  if (!items || !items.length) {
    return '<div class="workflow-footnote">No script distribution data.</div>';
  }
  return items
    .map(
      (item) => `
      <div class="distribution-row">
        <div class="distribution-head">
          <span>${escapeHtml(item.script_label || "other")}</span>
          <span>${escapeHtml(String(item.mentions_count || 0))} • ${escapeHtml(String(item.share_pct || 0))}%</span>
        </div>
        <div class="distribution-bar">
          <span style="width: ${Math.max(0, Math.min(100, Number(item.share_pct || 0)))}%"></span>
        </div>
      </div>
    `
    )
    .join("");
}

function renderVariantClusters(items) {
  if (!items || !items.length) {
    return '<div class="workflow-footnote">No variant clusters detected.</div>';
  }

  return items
    .map(
      (cluster) => `
      <div class="duplicate-card">
        <div class="duplicate-head">
          <span class="duplicate-path">${escapeHtml(cluster.normalized_name || "-")}</span>
          <span class="panel-pill">variants ${cluster.variants_count || 0}</span>
        </div>
        <div class="workflow-footnote">Docs ${cluster.docs_count || 0} • Mentions ${cluster.mentions_count || 0}</div>
        <div class="duplicate-items">
          ${(cluster.variants || [])
            .map(
              (variant) => `
              <div class="duplicate-item">
                <span>${escapeHtml(variant.raw_name || "-")} • ${escapeHtml(variant.script_label || "other")}</span>
                <span>${variant.docs_count || 0}</span>
              </div>
            `
            )
            .join("")}
        </div>
      </div>
    `
    )
    .join("");
}

function renderQueue(queue) {
  if (!queue || !queue.items || !queue.items.length) {
    return '<div class="workflow-footnote">Ambiguous queue is empty.</div>';
  }

  return `
    <div class="workflow-footnote">Total in queue: ${queue.total || 0}</div>
    ${queue.items
      .map(
        (item) => `
        <div class="run-row">
          <div>${escapeHtml(item.raw_name || "-")} • ${escapeHtml(item.script_label || "other")}</div>
          <div>${escapeHtml((item.reasons || []).join(", ") || "manual_review")} • docs ${item.docs_count || 0}</div>
        </div>
      `
      )
      .join("")}
  `;
}

function renderInsights(payload) {
  state.insightsPayload = payload;
  const scriptsNode = document.getElementById("scripts-root");
  const clustersNode = document.getElementById("clusters-root");
  const queueNode = document.getElementById("queue-root");

  if (!payload.available) {
    const errorHtml = renderWorkflowFootnoteMessage(payload.error || "unknown error", {
      error: true,
    });
    scriptsNode.innerHTML = errorHtml;
    clustersNode.innerHTML = errorHtml;
    queueNode.innerHTML = errorHtml;
    document.getElementById("tab-badge-scripts").textContent = "0";
    document.getElementById("tab-badge-clusters").textContent = "0";
    document.getElementById("tab-badge-queue").textContent = "0";
    return;
  }

  const summary = payload.summary || {};
  const scriptMentionsTotal = Number(
    summary.script_total_mentions
    ?? (payload.script_distribution || []).reduce(
      (sum, row) => sum + Number(row.mentions_count || 0),
      0
    )
  );
  const variantClusterCount = Number(summary.variant_cluster_count ?? (payload.variant_clusters || []).length);
  const ambiguousQueueTotal = Number(
    summary.ambiguous_queue_total ?? (payload.ambiguous_queue?.total || 0)
  );

  scriptsNode.innerHTML = renderDistribution(payload.script_distribution || []);
  clustersNode.innerHTML = renderVariantClusters(payload.variant_clusters || []);
  queueNode.innerHTML = renderQueue(payload.ambiguous_queue || {});
  document.getElementById("tab-badge-scripts").textContent = String(scriptMentionsTotal);
  document.getElementById("tab-badge-clusters").textContent = String(variantClusterCount);
  document.getElementById("tab-badge-queue").textContent = String(ambiguousQueueTotal);
}

function applyActiveTab() {
  tabController.apply();
}

function switchTab(tab) {
  tabController.select(tab);
}

async function refreshOverview() {
  const payload = await api("/api/library/personalities");
  state.globalPayload = payload;
  renderGlobalState(payload);
  renderOverview(payload.overview || {});
}

async function refreshTable() {
  const payload = await api(tableUrl());
  renderTable(payload);
}

async function refreshInsights() {
  const payload = await api("/api/library/personalities/insights");
  renderInsights(payload);
}

async function refreshAll() {
  await refreshAllWithState({});
}

function renderPageLoading() {
  viewState.set("loading");
  setStatusMessage(document.getElementById("personality-status"), "Loading personalities...", {
    error: false,
  });
  document.getElementById("personality-stat-grid").innerHTML = renderRunRowMessage("Loading overview...");
  document.getElementById("personality-top-list").innerHTML = renderRunRowMessage(
    "Loading top personalities..."
  );
  setStatusMessage(document.getElementById("personality-table-status"), "Loading personality table...", {
    error: false,
  });
  document.getElementById("personality-table-body").innerHTML =
    renderLoadingTableRow(6, "Loading personality rows...");
  document.getElementById("scripts-root").innerHTML = renderWorkflowFootnoteMessage(
    "Loading script distribution..."
  );
  document.getElementById("clusters-root").innerHTML = renderWorkflowFootnoteMessage(
    "Loading variant clusters..."
  );
  document.getElementById("queue-root").innerHTML = renderWorkflowFootnoteMessage(
    "Loading ambiguous queue..."
  );
}

function renderPageError(error) {
  viewState.set("error");
  const message = String(error?.message || error || "Failed to load personalities.");
  setStatusMessage(document.getElementById("personality-status"), `Personalities unavailable: ${message}`, {
    error: true,
  });
  document.getElementById("personality-stat-grid").innerHTML =
    renderRunRowMessage(message, { error: true });
  document.getElementById("personality-top-list").innerHTML =
    renderRunRowMessage(message, { error: true });
  setStatusMessage(document.getElementById("personality-table-status"), `Table unavailable: ${message}`, {
    error: true,
  });
  document.getElementById("personality-table-body").innerHTML = "";
  const errorHtml = renderWorkflowFootnoteMessage(message, { error: true });
  document.getElementById("scripts-root").innerHTML = errorHtml;
  document.getElementById("clusters-root").innerHTML = errorHtml;
  document.getElementById("queue-root").innerHTML = errorHtml;
  document.getElementById("tab-badge-scripts").textContent = "0";
  document.getElementById("tab-badge-clusters").textContent = "0";
  document.getElementById("tab-badge-queue").textContent = "0";
}

async function refreshAllWithState({ showLoading = false } = {}) {
  if (showLoading) {
    renderPageLoading();
  }
  try {
    await Promise.all([refreshOverview(), refreshTable(), refreshInsights()]);
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
    const confirmed = await window.ManzaraUI.confirm({
      title: "Force stop all tasks",
      message: "Running tasks will be terminated immediately without waiting for a safe boundary.",
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
    initialCursor: window.ManzaraCore.eventCursorFromSnapshot(state.globalPayload),
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
  window.ManzaraUI.toast(error.message || String(error), { tone: "error" });
});
