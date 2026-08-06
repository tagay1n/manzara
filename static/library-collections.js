const state = {
  overviewPayload: null,
  globalPayload: null,
  tablePayload: null,
  mode: "review_ready",
  page: 1,
  pageSize: 25,
  expandedId: null,
  reviews: {},
  eventCursor: 0,
  eventStreamController: null,
  soundNotifier: null,
};

const api = (path, options = {}) => window.ManzaraCore.api(path, options);
const escapeHtml = (value) => window.ManzaraCore.escapeHtml(value);
const message = (text, options = {}) => window.ManzaraCore.renderWorkflowFootnoteMessage(text, options);
const canReviewProposal = () => ["review_ready", "ai_dismissed"].includes(state.mode);

function renderGlobal(payload) {
  document.getElementById("global-status").textContent = window.ManzaraCore.formatGlobalStatus(
    payload.global.active_tasks || 0,
    payload.global.active_workflows || 0,
  );
  window.ManzaraCore.applyStopAllButton(
    document.getElementById("stop-all-btn"),
    payload.global.stop_all_state,
  );
}

function renderOverview(overview) {
  const stats = overview.stats || {};
  document.getElementById("collections-status").textContent = overview.available
    ? "Collection state is current."
    : `Collections unavailable: ${overview.error || "unknown error"}`;
  document.getElementById("collections-stat-grid").innerHTML = `
    <div class="library-stat-card"><span class="library-stat-label">Needs review</span><strong class="library-stat-value">${Number(stats.suggested_collections || 0)}</strong></div>
    <div class="library-stat-card"><span class="library-stat-label">Awaiting AI</span><strong class="library-stat-value">${Number(stats.awaiting_validation || 0)}</strong></div>
    <div class="library-stat-card"><span class="library-stat-label">Collections</span><strong class="library-stat-value">${Number(stats.approved_collections || 0)}</strong></div>
    <div class="library-stat-card"><span class="library-stat-label">Members</span><strong class="library-stat-value">${Number(stats.items_linked || 0)}</strong></div>`;
}

function listUrl() {
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
    search: document.getElementById("filter-search").value.trim(),
  });
  if (state.mode === "collections") {
    params.set("sort", "updated_desc");
    return `/api/library/collections/table?${params.toString()}`;
  }
  params.set("status", state.mode);
  return `/api/library/collection-proposals?${params.toString()}`;
}

function proposalCard(item) {
  const id = Number(item.proposal_id || 0);
  const expanded = state.expandedId === id;
  const kind = item.proposal_type === "attach_to_collection" ? "Addition" : "New collection";
  return `<article class="collection-queue-card${expanded ? " is-expanded" : ""}">
    <div class="collection-queue-head">
      <button class="collection-queue-trigger" data-proposal-toggle="${id}" aria-expanded="${expanded}">
        <span class="collection-queue-copy">
          <span class="collection-queue-title">${escapeHtml(item.title || "-")}</span>
          <span class="collection-queue-meta">${kind} · ${Number(item.item_count || 0)} documents · ${Math.round(Number(item.confidence || 0) * 100)}%</span>
        </span>
        <span class="collection-queue-tail"><span class="panel-pill">${escapeHtml(item.status || "-")}</span><i data-lucide="chevron-down"></i></span>
      </button>
      ${canReviewProposal() ? `<button class="small-btn collection-quick-reject" data-proposal-reject="${id}">Reject</button>` : ""}
    </div>
    ${expanded ? reviewDetails(id) : ""}
  </article>`;
}

function collectionCard(item) {
  return `<article class="collection-queue-card">
    <div class="collection-queue-head"><div class="collection-queue-trigger collection-static-row">
      <span class="collection-queue-copy"><span class="collection-queue-title">${escapeHtml(item.title || "-")}</span><span class="collection-queue-meta">${Number(item.item_count || 0)} accepted documents</span></span>
      <span class="panel-pill">approved</span>
    </div></div>
  </article>`;
}

function reviewDetails(id) {
  const payload = state.reviews[String(id)];
  if (!payload) return `<div class="collection-queue-details"><div class="workflow-footnote">Loading evidence...</div></div>`;
  if (!payload.available) return `<div class="collection-queue-details">${message(payload.error || "Review unavailable", { error: true })}</div>`;
  const proposal = payload.proposal || {};
  const items = (payload.items || []).map((item) => {
    const checked = item.selected_by_default ? "checked" : "";
    const confidence = item.confidence == null ? "not validated" : `${Math.round(Number(item.confidence) * 100)}%`;
    return `<label class="collection-proposal-item">
      <input type="checkbox" data-proposal-item="${id}" value="${escapeHtml(item.md5)}" ${checked} />
      <span class="collection-proposal-item-copy"><strong>${escapeHtml(item.title || item.md5)}</strong>
        <small>${escapeHtml([item.publication_date, item.issue_number && `Issue ${item.issue_number}`, ...(item.publishers || [])].filter(Boolean).join(" · ") || "Metadata only")}</small>
        <span>${escapeHtml(item.rationale || "No Gemini rationale")}</span>
      </span>
      <span class="collection-proposal-item-tail"><span class="panel-pill">${escapeHtml(item.verdict || "pending")}</span><small>${confidence}</small><a class="small-btn" href="/api/library/documents/${encodeURIComponent(item.md5)}/open" target="_blank" rel="noopener">Open</a></span>
    </label>`;
  }).join("");
  return `<div class="collection-queue-details">
    <div class="collection-review-safety"><i data-lucide="shield-check"></i><div><strong>Review proposal only</strong><span>Detection and Gemini have not changed collection membership or document metadata.</span></div></div>
    <div class="collection-proposal-summary"><strong>${escapeHtml(proposal.rationale || "Gemini validation complete")}</strong><span>Select the exact documents to accept.</span></div>
    <div class="collection-proposal-items">${items || message("No proposal items available.")}</div>
    ${canReviewProposal() ? `<div class="collection-queue-actions"><button class="small-btn collection-review-approve" data-proposal-approve="${id}">Approve selected</button></div>` : ""}
  </div>`;
}

function renderList(payload) {
  state.tablePayload = payload;
  const root = document.getElementById("collections-list-root");
  if (!payload.available) {
    root.innerHTML = message(payload.error || "Collections unavailable", { error: true });
    return;
  }
  document.getElementById("collections-list-status").textContent = payload.total
    ? `${payload.total} ${state.mode === "collections" ? "collections" : "proposals"}`
    : "Nothing in this view.";
  root.innerHTML = (payload.items || []).map(state.mode === "collections" ? collectionCard : proposalCard).join("") || '<div class="collections-review-empty">Nothing needs attention here.</div>';
  window.ManzaraCore.applyPaginationControls({
    labelNode: document.getElementById("page-label"),
    prevNode: document.getElementById("page-prev"),
    nextNode: document.getElementById("page-next"),
    page: payload.page,
    totalPages: payload.total_pages,
  });
  lucide.createIcons();
}

async function refreshOverview() {
  const payload = await api("/api/library/collections");
  state.globalPayload = payload;
  renderGlobal(payload);
  renderOverview(payload.overview || {});
}

async function refreshList() {
  renderList(await api(listUrl()));
}

async function refreshAll() {
  try {
    await Promise.all([refreshOverview(), refreshList()]);
  } catch (error) {
    const text = `Collections unavailable: ${String(error?.message || error)}`;
    document.getElementById("collections-status").textContent = text;
    document.getElementById("collections-list-status").textContent = text;
    document.getElementById("collections-list-root").innerHTML = message(text, { error: true });
    throw error;
  }
}

async function toggleProposal(id) {
  id = Number(id);
  if (state.expandedId === id) {
    state.expandedId = null;
    renderList(state.tablePayload);
    return;
  }
  state.expandedId = id;
  renderList(state.tablePayload);
  const payload = await api(`/api/library/collection-proposals/${id}`);
  state.reviews[String(id)] = payload;
  renderList(state.tablePayload);
}

async function decideProposal(id, decision) {
  const payload = state.reviews[String(id)];
  const selected = decision === "approve"
    ? [...document.querySelectorAll(`[data-proposal-item="${id}"]:checked`)].map((node) => node.value)
    : [];
  const title = payload?.proposal?.title || "This proposal";
  const confirmed = await window.ManzaraUI.confirm({
    title: decision === "approve" ? "Approve collection proposal" : "Reject collection proposal",
    message: decision === "approve" ? `${selected.length} selected document(s) will become authoritative members of ${title}.` : `${title} will be dismissed until its metadata evidence changes.`,
    acceptLabel: decision === "approve" ? "Approve selected" : "Reject",
    destructive: decision === "reject",
  });
  if (!confirmed) return;
  await api(`/api/library/collection-proposals/${id}/decision`, {
    method: "POST",
    body: JSON.stringify({ decision, selected_md5s: selected }),
  });
  delete state.reviews[String(id)];
  state.expandedId = null;
  await refreshAll();
  window.ManzaraUI.toast(decision === "approve" ? "Collection proposal approved." : "Collection proposal rejected.");
}

function setupEvents() {
  state.eventStreamController = window.ManzaraCore.createSseController({
    eventTypes: window.ManzaraCore.DEFAULT_EVENT_TYPES,
    initialCursor: window.ManzaraCore.eventCursorFromSnapshot(state.globalPayload),
    getCursor: () => Number(state.eventCursor || 0),
    setCursor: (cursor) => { state.eventCursor = Number(cursor || 0); },
    onEvent: (payload, event) => {
      document.getElementById("last-event").textContent = window.ManzaraCore.formatEventBanner(payload);
      state.soundNotifier?.handleEvent(payload, event.lastEventId || "");
      if (String(payload?.type || "").startsWith("library.") || String(payload?.panel_id || "") === "library") {
        window.ManzaraCore.scheduleRefresh(state, refreshAll, 150);
      }
    },
  });
  state.eventStreamController.start();
}

function attachHandlers() {
  document.querySelectorAll("[data-collection-mode]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll("[data-collection-mode]").forEach((item) => item.classList.toggle("is-active", item === button));
    state.mode = button.dataset.collectionMode;
    state.page = 1;
    state.expandedId = null;
    refreshList().catch(console.error);
  }));
  document.getElementById("filter-apply").addEventListener("click", () => { state.page = 1; refreshList().catch(console.error); });
  document.getElementById("page-prev").addEventListener("click", () => { state.page = Math.max(1, state.page - 1); refreshList().catch(console.error); });
  document.getElementById("page-next").addEventListener("click", () => { state.page = Math.min(Number(state.tablePayload?.total_pages || 1), state.page + 1); refreshList().catch(console.error); });
  document.getElementById("collections-list-root").addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-proposal-toggle]");
    const approve = event.target.closest("[data-proposal-approve]");
    const reject = event.target.closest("[data-proposal-reject]");
    if (approve) decideProposal(Number(approve.dataset.proposalApprove), "approve").catch(console.error);
    else if (reject) decideProposal(Number(reject.dataset.proposalReject), "reject").catch(console.error);
    else if (toggle) toggleProposal(toggle.dataset.proposalToggle).catch(console.error);
  });
  document.getElementById("stop-all-btn").addEventListener("click", () => api("/api/system/stop-all", { method: "POST" }).catch(console.error));
}

async function init() {
  state.soundNotifier = window.ManzaraSound?.createNotifier?.({ repeatGapMs: 2000 }) || null;
  attachHandlers();
  await refreshAll();
  setupEvents();
}

window.addEventListener("beforeunload", () => {
  state.eventStreamController?.stop();
  state.soundNotifier?.teardown?.();
});
init().catch(console.error);
