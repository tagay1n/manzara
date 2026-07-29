const state = {
  overviewPayload: null,
  tablePayload: null,
  insightsPayload: null,
  globalPayload: null,
  selectedCollectionId: null,
  selectedCollection: null,
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

const TAB_IDS = ["table", "clusters", "queue"];

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

function renderWorkflowFootnoteMessage(text, options = {}) {
  return window.ManzaraCore.renderWorkflowFootnoteMessage(text, options);
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
    activeWorkflows,
  );
  const stopBtn = document.getElementById("stop-all-btn");
  window.ManzaraCore.applyStopAllButton(stopBtn, payload.global.stop_all_state);
}

function currentFilters() {
  return {
    search: document.getElementById("filter-search").value.trim(),
    status: document.getElementById("filter-status").value,
    include: document.getElementById("filter-include").value,
    sort: document.getElementById("filter-sort").value,
  };
}

function tableUrl() {
  const filters = currentFilters();
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
    search: filters.search,
    status: filters.status,
    include: filters.include,
    sort: filters.sort,
  });
  return `/api/library/collections/table?${params.toString()}`;
}

function renderOverview(overview) {
  state.overviewPayload = overview;
  const statusNode = document.getElementById("collections-status");
  const stats = overview.stats || {};
  if (overview.available) {
    const source = overview.config_source ? ` • source: ${overview.config_source}` : "";
    setStatusMessage(statusNode, `Collection stats loaded${source}`, { error: false });
  } else {
    setStatusMessage(
      statusNode,
      `Collections unavailable: ${overview.error || "unknown error"}`,
      { error: true },
    );
  }

  document.getElementById("collections-stat-grid").innerHTML = `
    <div class="library-stat-card"><span class="library-stat-label">Collections</span><span class="library-stat-value">${stats.total_collections || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Suggested</span><span class="library-stat-value">${stats.suggested_collections || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Approved</span><span class="library-stat-value">${stats.approved_collections || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Included</span><span class="library-stat-value">${stats.included_collections || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Linked Items</span><span class="library-stat-value">${stats.items_linked || 0}</span></div>
  `;

  const topRows = (overview.top_collections || [])
    .slice(0, 12)
    .map(
      (item) => `
      <div class="library-top-row">
        <div class="library-top-main">
          <span class="library-top-ddc">${escapeHtml(item.title || "-")}</span>
          <span class="library-top-path">${escapeHtml(item.status || "-")} • confidence ${(Number(item.confidence || 0) * 100).toFixed(0)}%</span>
        </div>
        <span class="library-top-count">${escapeHtml(String(item.item_count || 0))}</span>
      </div>
    `,
    )
    .join("");
  document.getElementById("collections-top-list").innerHTML =
    topRows || '<div class="run-row">No collection candidates yet.</div>';
}

function renderTable(payload) {
  state.tablePayload = payload;
  const statusNode = document.getElementById("collections-table-status");
  if (!payload.available) {
    setStatusMessage(statusNode, `Table unavailable: ${payload.error || "unknown error"}`, {
      error: true,
    });
    document.getElementById("collections-table-body").innerHTML = "";
    return;
  }

  setStatusMessage(statusNode, `Loaded ${payload.items.length} rows from ${payload.total} total`, {
    error: false,
  });
  document.getElementById("collections-table-body").innerHTML = (payload.items || [])
    .map((item) => {
      const collectionId = Number(item.collection_id || 0);
      const selectedClass =
        state.selectedCollectionId !== null && Number(state.selectedCollectionId) === collectionId
          ? ' class="table-row-selected"'
          : "";
      return `
      <tr${selectedClass}>
        <td>${collectionId || "-"}</td>
        <td>
          <button class="small-btn" data-collection-id="${collectionId}" data-collection-action="select">${escapeHtml(item.title || "-")}</button>
        </td>
        <td>${escapeHtml(item.status || "-")}</td>
        <td>${item.include_in_library ? "yes" : "no"}</td>
        <td>${Math.round(Number(item.confidence || 0) * 100)}%</td>
        <td>${Number(item.item_count || 0)}</td>
        <td>${escapeHtml(formatDateTime(item.last_detected_at))}</td>
      </tr>
    `;
    })
    .join("");

  window.ManzaraCore.applyPaginationControls({
    labelNode: document.getElementById("page-label"),
    prevNode: document.getElementById("page-prev"),
    nextNode: document.getElementById("page-next"),
    page: payload.page,
    totalPages: payload.total_pages,
  });
}

function renderClusters(items) {
  if (!items || !items.length) {
    return '<div class="workflow-footnote">No clusters available.</div>';
  }
  return items
    .map(
      (item) => `
      <div class="duplicate-card">
        <div class="duplicate-head">
          <span class="duplicate-path">${escapeHtml(item.title || "-")}</span>
          <span class="panel-pill">${escapeHtml(item.status || "-")}</span>
        </div>
        <div class="workflow-footnote">Items ${item.item_count || 0} • confidence ${Math.round(Number(item.confidence || 0) * 100)}%</div>
      </div>
    `,
    )
    .join("");
}

function renderQueue(queue) {
  if (!queue || !queue.items || !queue.items.length) {
    return '<div class="workflow-footnote">Review queue is empty.</div>';
  }
  return `
    <div class="workflow-footnote">Total in queue: ${queue.total || 0}</div>
    ${(queue.items || [])
      .map(
        (item) => `
        <div class="run-row">
          <div>${escapeHtml(item.title || "-")} • ${escapeHtml(item.status || "-")}</div>
          <div>${item.item_count || 0} items • ${Math.round(Number(item.confidence || 0) * 100)}%</div>
        </div>
      `,
      )
      .join("")}
  `;
}

function renderInsights(payload) {
  state.insightsPayload = payload;
  const clustersNode = document.getElementById("clusters-root");
  const queueNode = document.getElementById("queue-root");
  if (!payload.available) {
    const errorHtml = renderWorkflowFootnoteMessage(payload.error || "unknown error", { error: true });
    clustersNode.innerHTML = errorHtml;
    queueNode.innerHTML = errorHtml;
    document.getElementById("tab-badge-clusters").textContent = "0";
    document.getElementById("tab-badge-queue").textContent = "0";
    return;
  }

  const summary = payload.summary || {};
  clustersNode.innerHTML = renderClusters(payload.clusters || []);
  queueNode.innerHTML = renderQueue(payload.queue || {});
  document.getElementById("tab-badge-clusters").textContent = String(summary.cluster_count || 0);
  document.getElementById("tab-badge-queue").textContent = String(summary.queue_total || 0);
}

function renderCollectionItems(payload) {
  const statusNode = document.getElementById("collection-items-status");
  if (!payload.available) {
    setStatusMessage(statusNode, `Collection items unavailable: ${payload.error || "unknown error"}`, {
      error: true,
    });
    document.getElementById("collection-items-body").innerHTML = "";
    return;
  }

  state.selectedCollection = payload.collection || null;
  const collection = payload.collection || {};
  document.getElementById("collection-title-input").value = String(collection.title || "");
  document.getElementById("collection-notes-input").value = String(collection.notes || "");
  setStatusMessage(
    statusNode,
    `Loaded ${payload.items.length} items for collection ${collection.collection_id || "-"}`,
    { error: false },
  );
  document.getElementById("collection-items-body").innerHTML = (payload.items || [])
    .map(
      (item) => `
      <tr>
        <td>${escapeHtml(item.md5 || "-")}</td>
        <td>${escapeHtml(item.item_title || item.schema_name || "-")}</td>
        <td title="${escapeHtml(item.ya_path || item.document_url || "-")}">${escapeHtml(item.ya_path || item.document_url || "-")}</td>
        <td>${item.lib ? "true" : "false"}</td>
      </tr>
    `,
    )
    .join("");
}

function applyActiveTab() {
  tabController.apply();
}

function switchTab(tab) {
  tabController.select(tab);
}

async function refreshOverview() {
  const payload = await api("/api/library/collections");
  state.globalPayload = payload;
  renderGlobalState(payload);
  renderOverview(payload.overview || {});
}

async function refreshTable() {
  const payload = await api(tableUrl());
  renderTable(payload);
}

async function refreshInsights() {
  const payload = await api("/api/library/collections/insights");
  renderInsights(payload);
}

async function refreshAll() {
  viewState.set("loading");
  try {
    await Promise.all([refreshOverview(), refreshTable(), refreshInsights()]);
    viewState.set("ready");
  } catch (error) {
    viewState.set("error");
    const message = String(error?.message || error || "Failed to load collections");
    setStatusMessage(document.getElementById("collections-status"), `Collections unavailable: ${message}`, {
      error: true,
    });
    setStatusMessage(
      document.getElementById("collections-table-status"),
      `Table unavailable: ${message}`,
      { error: true },
    );
    document.getElementById("clusters-root").innerHTML = renderWorkflowFootnoteMessage(message, {
      error: true,
    });
    document.getElementById("queue-root").innerHTML = renderWorkflowFootnoteMessage(message, {
      error: true,
    });
    throw error;
  }
}

async function loadCollectionItems(collectionId) {
  const id = Number(collectionId || 0);
  if (!id) return;
  state.selectedCollectionId = id;
  const payload = await api(`/api/library/collections/${encodeURIComponent(String(id))}/items`);
  renderCollectionItems(payload);
  await refreshTable();
}

async function patchSelectedCollection(patch) {
  const id = Number(state.selectedCollectionId || 0);
  if (!id) {
    window.ManzaraUI.toast("Select a collection first.", { tone: "warning" });
    return;
  }
  const titleValue = document.getElementById("collection-title-input").value.trim();
  const notesValue = document.getElementById("collection-notes-input").value.trim();
  const payload = {
    ...patch,
    title: titleValue,
    notes: notesValue,
  };
  await api(`/api/library/collections/${encodeURIComponent(String(id))}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
  await Promise.all([refreshOverview(), refreshTable(), refreshInsights()]);
  await loadCollectionItems(id);
}

function queueRefresh(delayMs = 250) {
  window.ManzaraCore.scheduleRefresh(state, async () => {
    await refreshAll();
    if (state.selectedCollectionId) {
      await loadCollectionItems(state.selectedCollectionId);
    }
  }, delayMs);
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
        queueRefresh(100);
      }
    },
  });
  state.eventStreamController.start();
}

function attachUiHandlers() {
  for (const tab of TAB_IDS) {
    const node = document.getElementById(`tab-btn-${tab}`);
    if (!node) continue;
    node.addEventListener("click", () => switchTab(tab));
  }

  document.getElementById("filter-apply").addEventListener("click", () => {
    state.page = 1;
    refreshTable().catch((error) => console.error(error));
  });
  document.getElementById("page-prev").addEventListener("click", () => {
    state.page = Math.max(1, state.page - 1);
    refreshTable().catch((error) => console.error(error));
  });
  document.getElementById("page-next").addEventListener("click", () => {
    const totalPages = Math.max(1, Number(state.tablePayload?.total_pages || 1));
    state.page = Math.min(totalPages, state.page + 1);
    refreshTable().catch((error) => console.error(error));
  });
  document.getElementById("stop-all-btn").addEventListener("click", () => {
    stopAll().catch((error) => console.error(error));
  });

  document.getElementById("collections-table-body").addEventListener("click", (event) => {
    const target = event.target;
    const action = target?.getAttribute?.("data-collection-action");
    if (action !== "select") return;
    const collectionId = target?.getAttribute?.("data-collection-id");
    if (!collectionId) return;
    loadCollectionItems(Number(collectionId)).catch((error) => console.error(error));
  });

  document.getElementById("collection-approve-btn").addEventListener("click", () => {
    patchSelectedCollection({ status: "approved" }).catch((error) => console.error(error));
  });
  document.getElementById("collection-reject-btn").addEventListener("click", () => {
    patchSelectedCollection({ status: "rejected" }).catch((error) => console.error(error));
  });
  document.getElementById("collection-include-btn").addEventListener("click", () => {
    const include = !(state.selectedCollection && state.selectedCollection.include_in_library);
    patchSelectedCollection({ include_in_library: include }).catch((error) => console.error(error));
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
  await refreshAll();
  applyActiveTab();
  setupEventStream();
  lucide.createIcons();
}

bootstrap().catch((error) => {
  console.error(error);
  window.ManzaraUI.toast(error.message || String(error), { tone: "error" });
});
