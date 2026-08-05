const state = {
  overviewPayload: null,
  tablePayload: null,
  globalPayload: null,
  viewState: "loading",
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  soundNotifier: null,
  page: 1,
  pageSize: 25,
  expandedCollectionId: null,
  reviewPayloads: {},
  reviewErrors: {},
  reviewLoading: {},
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

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

function initSoundNotifier() {
  const createNotifier = window.ManzaraSound?.createNotifier;
  if (typeof createNotifier === "function") {
    state.soundNotifier = createNotifier({ repeatGapMs: 2000 });
  }
}

function teardownSoundNotifier() {
  state.soundNotifier?.teardown?.();
  state.soundNotifier = null;
}

function renderGlobalState(payload) {
  const active = payload.global.active_tasks || 0;
  const activeWorkflows = payload.global.active_workflows || 0;
  document.getElementById("global-status").textContent = window.ManzaraCore.formatGlobalStatus(
    active,
    activeWorkflows,
  );
  window.ManzaraCore.applyStopAllButton(
    document.getElementById("stop-all-btn"),
    payload.global.stop_all_state,
  );
}

function tableUrl() {
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
    search: document.getElementById("filter-search").value.trim(),
    status: document.getElementById("filter-status").value,
    include: "all",
    sort: document.getElementById("filter-sort").value,
  });
  return `/api/library/collections/table?${params.toString()}`;
}

function renderOverview(overview) {
  state.overviewPayload = overview;
  const stats = overview.stats || {};
  if (overview.available) {
    setStatusMessage(document.getElementById("collections-status"), "Review state is current.");
  } else {
    setStatusMessage(
      document.getElementById("collections-status"),
      `Collections unavailable: ${overview.error || "unknown error"}`,
      { error: true },
    );
  }
  document.getElementById("collections-stat-grid").innerHTML = `
    <div class="library-stat-card"><span class="library-stat-label">To review</span><span class="library-stat-value">${Number(stats.suggested_collections || 0)}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Approved</span><span class="library-stat-value">${Number(stats.approved_collections || 0)}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Linked items</span><span class="library-stat-value">${Number(stats.items_linked || 0)}</span></div>
  `;
}

function renderCollectionList(payload) {
  state.tablePayload = payload;
  const statusNode = document.getElementById("collections-list-status");
  const root = document.getElementById("collections-list-root");
  if (!payload.available) {
    const message = `Collections unavailable: ${payload.error || "unknown error"}`;
    setStatusMessage(statusNode, message, { error: true });
    root.innerHTML = renderWorkflowFootnoteMessage(message, { error: true });
    return;
  }

  const items = payload.items || [];
  setStatusMessage(
    statusNode,
    items.length ? `${payload.total} collections` : "No collections match this view.",
  );
  root.innerHTML = items.length
    ? items.map(renderCollectionCard).join("")
    : '<div class="collections-review-empty">Nothing needs attention here.</div>';

  window.ManzaraCore.applyPaginationControls({
    labelNode: document.getElementById("page-label"),
    prevNode: document.getElementById("page-prev"),
    nextNode: document.getElementById("page-next"),
    page: payload.page,
    totalPages: payload.total_pages,
  });
}

function renderCollectionCard(item) {
  const collectionId = Number(item.collection_id || 0);
  const expanded = collectionId > 0 && state.expandedCollectionId === collectionId;
  return `
    <article class="collection-queue-card${expanded ? " is-expanded" : ""}">
      <button
        type="button"
        class="collection-queue-trigger"
        data-queue-collection-id="${collectionId}"
        aria-expanded="${expanded ? "true" : "false"}"
        aria-controls="collection-queue-details-${collectionId}"
      >
        <span class="collection-queue-copy">
          <span class="collection-queue-title">${escapeHtml(item.title || "-")}</span>
          <span class="collection-queue-meta">${Number(item.item_count || 0)} items · ${Math.round(Number(item.confidence || 0) * 100)}% confidence</span>
        </span>
        <span class="collection-queue-tail">
          <span class="panel-pill">${escapeHtml(item.status || "-")}</span>
          <i data-lucide="chevron-down" aria-hidden="true"></i>
        </span>
      </button>
      ${expanded ? renderReviewDetails(collectionId) : ""}
    </article>
  `;
}

function renderReviewDetails(collectionId) {
  const key = String(collectionId);
  if (state.reviewLoading[key]) {
    return `<div id="collection-queue-details-${collectionId}" class="collection-queue-details"><div class="workflow-footnote">Loading evidence...</div></div>`;
  }
  if (state.reviewErrors[key]) {
    return `<div id="collection-queue-details-${collectionId}" class="collection-queue-details">${renderWorkflowFootnoteMessage(state.reviewErrors[key], { error: true })}</div>`;
  }
  const payload = state.reviewPayloads[key];
  if (!payload) {
    return `<div id="collection-queue-details-${collectionId}" class="collection-queue-details"><div class="workflow-footnote">Evidence is not loaded.</div></div>`;
  }
  if (!payload.available) {
    return `<div id="collection-queue-details-${collectionId}" class="collection-queue-details">${renderWorkflowFootnoteMessage(payload.error || "Review unavailable", { error: true })}</div>`;
  }

  const summary = payload.summary || {};
  const safety = payload.safety || {};
  const evidence = (payload.grouping_evidence || [])
    .map(
      (item) => `
        <div class="collection-review-evidence-row">
          <span>${escapeHtml(item.label || item.key || "Evidence")}</span>
          <strong>${escapeHtml(item.value || "-")}</strong>
        </div>`,
    )
    .join("");
  const consistency = Object.entries(payload.consistency || {})
    .map(
      ([key, item]) => `
        <div class="collection-review-metric">
          <span>${escapeHtml(reviewMetricLabel(key))}</span>
          <strong>${Number(item?.percent || 0).toFixed(0)}%</strong>
          <small>${escapeHtml(item?.dominant || "No metadata")} · ${Number(item?.distinct || 0)} variants</small>
        </div>`,
    )
    .join("");
  const outliers = (payload.outliers || []).map(renderReviewItem).join("");
  const samples = (payload.samples || []).map(renderReviewItem).join("");

  return `
    <div id="collection-queue-details-${collectionId}" class="collection-queue-details">
      <div class="collection-review-safety">
        <i data-lucide="shield-check" aria-hidden="true"></i>
        <div>
          <strong>${escapeHtml(safety.approval_effect || "Approval records the review decision only")}</strong>
          <span>Document metadata changes only when the separate override task runs.</span>
        </div>
      </div>
      <div class="collection-review-summary">
        ${renderReviewStat("Items", summary.item_count || 0)}
        ${renderReviewStat("Dates", `${Number(summary.date_coverage?.percent || 0).toFixed(0)}%`)}
        ${renderReviewStat("Issue numbers", `${Number(summary.issue_number_coverage?.percent || 0).toFixed(0)}%`)}
        ${renderReviewStat("Outliers", payload.outliers_total || 0)}
        ${renderReviewStat("Date range", reviewDateRange(summary.date_range || {}))}
      </div>
      <div class="collection-review-grid">
        <section class="collection-review-section">
          <h3>Why grouped</h3>
          <div class="collection-review-evidence">${evidence || '<div class="workflow-footnote">No grouping evidence.</div>'}</div>
        </section>
        <section class="collection-review-section">
          <h3>Consistency</h3>
          <div class="collection-review-metrics">${consistency || '<div class="workflow-footnote">No consistency data.</div>'}</div>
        </section>
      </div>
      <section class="collection-review-section collection-review-outliers">
        <div class="collection-review-section-head"><h3>Possible exceptions</h3><span class="panel-pill">${Number(payload.outliers_total || 0)}</span></div>
        <div class="collection-queue-items">${outliers || '<div class="workflow-footnote">No metadata outliers detected.</div>'}</div>
      </section>
      <section class="collection-review-section">
        <h3>Representative documents</h3>
        <div class="collection-queue-items">${samples || '<div class="workflow-footnote">No linked items.</div>'}</div>
      </section>
      <div class="collection-queue-actions collection-review-actions">
        <button type="button" class="small-btn" data-queue-decision="defer" data-queue-decision-id="${collectionId}">Leave for later</button>
        <button type="button" class="small-btn" data-queue-decision="reject" data-queue-decision-id="${collectionId}">Reject candidate</button>
        <button type="button" class="small-btn collection-review-approve" data-queue-decision="approve" data-queue-decision-id="${collectionId}">Approve candidate</button>
      </div>
    </div>
  `;
}

function renderReviewStat(label, value) {
  return `<div class="collection-review-stat"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong></div>`;
}

function reviewMetricLabel(key) {
  return {
    title: "Titles",
    publisher: "Publishers",
    genre: "Genres",
    work_type: "Document types",
    parent: "Source folders",
  }[String(key)] || String(key || "Metadata");
}

function reviewReasonLabel(reason) {
  return {
    title_mismatch: "Title differs",
    publisher_mismatch: "Publisher differs",
    genre_mismatch: "Genre differs",
    type_mismatch: "Document type differs",
    parent_mismatch: "Source folder differs",
    missing_date: "Date missing",
    missing_issue_number: "Issue number missing",
  }[String(reason)] || String(reason || "Unusual metadata");
}

function reviewDate(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${match[3]}.${match[2]}.${match[1]}` : String(value || "-");
}

function reviewDateRange(range) {
  if (!range?.earliest && !range?.latest) return "-";
  if (range.earliest === range.latest) return reviewDate(range.earliest);
  return `${reviewDate(range.earliest)} – ${reviewDate(range.latest)}`;
}

function renderReviewItem(item) {
  const reasons = (item.reasons || [])
    .map((reason) => `<span class="collection-review-reason">${escapeHtml(reviewReasonLabel(reason))}</span>`)
    .join("");
  const facts = [
    item.publication_date ? reviewDate(item.publication_date) : null,
    item.issue_number ? `Issue ${item.issue_number}` : null,
    item.publisher || null,
    item.genre || item.work_type || null,
    item.number_of_pages ? `${item.number_of_pages} pages` : null,
  ].filter(Boolean);
  return `
    <div class="collection-queue-item${reasons ? " has-outlier" : ""}">
      <div class="collection-queue-item-main">
        <span class="collection-queue-item-title">${escapeHtml(item.title || item.file_name || item.md5 || "-")}</span>
        <span class="collection-review-item-facts">${facts.map((fact) => escapeHtml(fact)).join(" · ") || "Metadata unavailable"}</span>
        <span class="collection-queue-item-path">${escapeHtml(item.file_name || item.path || "-")}</span>
        ${reasons ? `<span class="collection-review-reasons">${reasons}</span>` : ""}
      </div>
      <span class="panel-pill">${item.included ? "included" : "not included"}</span>
    </div>`;
}

async function refreshOverview() {
  const payload = await api("/api/library/collections");
  state.globalPayload = payload;
  renderGlobalState(payload);
  renderOverview(payload.overview || {});
}

async function refreshList() {
  renderCollectionList(await api(tableUrl()));
  lucide.createIcons();
}

async function refreshAll() {
  viewState.set("loading");
  try {
    await Promise.all([refreshOverview(), refreshList()]);
    viewState.set("ready");
  } catch (error) {
    viewState.set("error");
    const message = String(error?.message || error || "Failed to load collections");
    setStatusMessage(
      document.getElementById("collections-status"),
      `Collections unavailable: ${message}`,
      { error: true },
    );
    setStatusMessage(document.getElementById("collections-list-status"), message, { error: true });
    document.getElementById("collections-list-root").innerHTML = renderWorkflowFootnoteMessage(
      message,
      { error: true },
    );
    throw error;
  }
}

async function toggleCollection(collectionId) {
  const id = Number(collectionId || 0);
  if (!id) return;
  if (state.expandedCollectionId === id) {
    state.expandedCollectionId = null;
    renderCollectionList(state.tablePayload);
    lucide.createIcons();
    return;
  }

  state.expandedCollectionId = id;
  const key = String(id);
  if (state.reviewPayloads[key]) {
    renderCollectionList(state.tablePayload);
    lucide.createIcons();
    return;
  }

  state.reviewLoading[key] = true;
  delete state.reviewErrors[key];
  renderCollectionList(state.tablePayload);
  try {
    state.reviewPayloads[key] = await api(
      `/api/library/collections/${encodeURIComponent(key)}/review`,
    );
  } catch (error) {
    state.reviewErrors[key] = String(error?.message || error || "Failed to load evidence");
  } finally {
    state.reviewLoading[key] = false;
    renderCollectionList(state.tablePayload);
    lucide.createIcons();
  }
}

async function decideCollection(collectionId, decision) {
  const id = Number(collectionId || 0);
  if (!id) return;
  if (decision === "defer") {
    state.expandedCollectionId = null;
    renderCollectionList(state.tablePayload);
    lucide.createIcons();
    return;
  }

  const status = decision === "approve" ? "approved" : "rejected";
  await api(`/api/library/collections/${encodeURIComponent(String(id))}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  });
  delete state.reviewPayloads[String(id)];
  state.expandedCollectionId = null;
  await Promise.all([refreshOverview(), refreshList()]);
  window.ManzaraUI.toast(
    status === "approved" ? "Collection approved." : "Collection rejected.",
  );
}

function queueRefresh(delayMs = 250) {
  window.ManzaraCore.scheduleRefresh(state, refreshAll, delayMs);
}

async function stopAll() {
  if (state.globalPayload?.global?.stop_all_state === "armed") {
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
      state.soundNotifier?.handleEvent(payload, event.lastEventId || "");
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
  document.getElementById("filter-apply").addEventListener("click", () => {
    state.page = 1;
    refreshList().catch((error) => console.error(error));
  });
  document.getElementById("page-prev").addEventListener("click", () => {
    state.page = Math.max(1, state.page - 1);
    refreshList().catch((error) => console.error(error));
  });
  document.getElementById("page-next").addEventListener("click", () => {
    const totalPages = Math.max(1, Number(state.tablePayload?.total_pages || 1));
    state.page = Math.min(totalPages, state.page + 1);
    refreshList().catch((error) => console.error(error));
  });
  document.getElementById("stop-all-btn").addEventListener("click", () => {
    stopAll().catch((error) => console.error(error));
  });
  document.getElementById("collections-list-root").addEventListener("click", (event) => {
    const decisionTarget = event.target?.closest?.("[data-queue-decision]");
    if (decisionTarget) {
      decideCollection(
        decisionTarget.dataset.queueDecisionId,
        decisionTarget.dataset.queueDecision,
      ).catch((error) => console.error(error));
      return;
    }
    const toggleTarget = event.target?.closest?.("[data-queue-collection-id]");
    if (toggleTarget) {
      toggleCollection(toggleTarget.dataset.queueCollectionId).catch((error) => console.error(error));
    }
  });
}

async function bootstrap() {
  initSoundNotifier();
  window.addEventListener("beforeunload", () => {
    teardownSoundNotifier();
    state.eventStreamController?.stop();
    state.eventStreamController = null;
  });
  attachUiHandlers();
  await refreshAll();
  setupEventStream();
  lucide.createIcons();
}

bootstrap().catch((error) => {
  console.error(error);
  window.ManzaraUI.toast(error.message || String(error), { tone: "error" });
});
