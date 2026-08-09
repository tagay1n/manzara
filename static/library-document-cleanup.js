const cleanupState = {
  mode: "queue",
  overview: null,
  list: null,
  eventCursor: 0,
  eventStreamController: null,
};

const cleanupApi = (path, options = {}) => window.ManzaraCore.api(path, options);
const cleanupEscape = (value) => window.ManzaraCore.escapeHtml(value);

function renderCleanupStats(payload) {
  const stats = payload.stats || {};
  document.getElementById("cleanup-stat-grid").innerHTML = `
    <div class="library-stat-card"><span class="library-stat-label">Active plans</span><strong class="library-stat-value">${Number(stats.active_plans || 0)}</strong></div>
    <div class="library-stat-card"><span class="library-stat-label">ISBN reviews</span><strong class="library-stat-value">${Number(stats.pending_reviews || 0)}</strong></div>
    <div class="library-stat-card"><span class="library-stat-label">Failed</span><strong class="library-stat-value">${Number(stats.failed_plans || 0)}</strong></div>
    <div class="library-stat-card"><span class="library-stat-label">Completed</span><strong class="library-stat-value">${Number(stats.completed_plans || 0)}</strong></div>`;
}

function cleanupQueueCard(item) {
  const detail = item.last_error || item.source_path || item.md5;
  return `<article class="collection-queue-card cleanup-row">
    <div class="collection-static-row cleanup-row-main">
      <span class="cleanup-action-icon"><i data-lucide="${item.action === "move" ? "folder-input" : "trash-2"}"></i></span>
      <span class="collection-queue-copy">
        <span class="collection-queue-title">${cleanupEscape(item.reason || "Cleanup")}</span>
        <span class="collection-queue-meta">${cleanupEscape(detail)}</span>
      </span>
      <span class="collection-queue-tail"><span class="panel-pill">${cleanupEscape(item.status || "planned")}</span></span>
    </div>
  </article>`;
}

function cleanupReviewCard(item) {
  const candidates = (item.candidates_json || []).map((candidate) => `
    <label class="collection-proposal-item cleanup-review-item">
      <input type="checkbox" data-review-candidate="${Number(item.review_id)}" value="${cleanupEscape(candidate.md5)}" checked />
      <span class="collection-proposal-item-copy">
        <strong>${cleanupEscape(candidate.title || candidate.md5)}</strong>
        <small>${cleanupEscape([candidate.mime_type, candidate.full ? "complete" : "partial"].filter(Boolean).join(" · "))}</small>
        <span>${cleanupEscape(candidate.source_path || "")}</span>
      </span>
      <span class="collection-proposal-item-tail"><a class="small-btn" href="/api/library/documents/${encodeURIComponent(candidate.md5)}/open" target="_blank" rel="noopener">Open</a></span>
    </label>`).join("");
  return `<article class="collection-queue-card is-expanded cleanup-review-card">
    <div class="collection-static-row cleanup-review-head">
      <span class="collection-queue-copy"><span class="collection-queue-title">ISBN ${cleanupEscape(item.isbn)}</span><span class="collection-queue-meta">Select every document that must remain</span></span>
      <button class="small-btn primary" data-review-decide="${Number(item.review_id)}">Keep selected</button>
    </div>
    <div class="collection-queue-details"><div class="collection-proposal-items">${candidates}</div></div>
  </article>`;
}

function renderCleanupList(payload) {
  cleanupState.list = payload;
  const items = payload.items || [];
  document.getElementById("cleanup-status").textContent = items.length
    ? `${items.length} item${items.length === 1 ? "" : "s"}`
    : "Nothing needs attention in this view.";
  const renderer = cleanupState.mode === "reviews" ? cleanupReviewCard : cleanupQueueCard;
  document.getElementById("cleanup-list").innerHTML = items.map(renderer).join("")
    || '<div class="collections-review-empty">No items.</div>';
  window.lucide?.createIcons?.();
}

async function refreshCleanup() {
  const overview = await cleanupApi("/api/library/document-cleanup");
  cleanupState.overview = overview;
  cleanupState.eventCursor = Math.max(cleanupState.eventCursor, Number(overview.event_cursor || 0));
  renderCleanupStats(overview);
  let path = "/api/library/document-cleanup/queue?limit=200";
  if (cleanupState.mode === "reviews") path = "/api/library/document-cleanup/isbn-reviews?status=pending&limit=100";
  if (cleanupState.mode === "history") path = "/api/library/document-cleanup/queue?status=completed&limit=200";
  renderCleanupList(await cleanupApi(path));
}

async function decideCleanupReview(reviewId) {
  const selected = [...document.querySelectorAll(`[data-review-candidate="${reviewId}"]:checked`)]
    .map((node) => node.value);
  const all = [...document.querySelectorAll(`[data-review-candidate="${reviewId}"]`)];
  const removed = all.length - selected.length;
  const confirmed = await window.ManzaraUI.confirm({
    title: "Resolve duplicate ISBN",
    message: `${selected.length} document(s) will remain. ${removed} document(s) will be queued for verified cleanup.`,
    acceptLabel: "Save decision",
    destructive: removed > 0,
  });
  if (!confirmed) return;
  await cleanupApi(`/api/library/document-cleanup/isbn-reviews/${reviewId}/decision`, {
    method: "POST",
    body: JSON.stringify({ keep_md5s: selected }),
  });
  await refreshCleanup();
  window.ManzaraUI.toast("ISBN decision saved and cleanup plans created.");
}

function setupCleanupEvents() {
  cleanupState.eventStreamController = window.ManzaraCore.createSseController({
    eventTypes: [...window.ManzaraCore.DEFAULT_EVENT_TYPES, "library.document_cleanup_changed"],
    initialCursor: Number(cleanupState.overview?.event_cursor || 0),
    getCursor: () => cleanupState.eventCursor,
    setCursor: (cursor) => { cleanupState.eventCursor = Number(cursor || 0); },
    onEvent: (payload) => {
      document.getElementById("last-event").textContent = window.ManzaraCore.formatEventBanner(payload);
      const taskId = String(payload?.task_id || "");
      if (String(payload?.type || "") === "library.document_cleanup_changed"
          || taskId === "library.prepare_document_cleanup"
          || taskId === "maintenance.monocorpus_sync") {
        window.ManzaraCore.scheduleRefresh(cleanupState, refreshCleanup, 150);
      }
    },
  });
  cleanupState.eventStreamController.start();
}

document.querySelectorAll("[data-cleanup-mode]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-cleanup-mode]").forEach((item) => item.classList.toggle("is-active", item === button));
  cleanupState.mode = button.dataset.cleanupMode;
  refreshCleanup().catch(console.error);
}));
document.getElementById("cleanup-list").addEventListener("click", (event) => {
  const action = event.target.closest("[data-review-decide]");
  if (action) decideCleanupReview(Number(action.dataset.reviewDecide)).catch(console.error);
});
window.addEventListener("beforeunload", () => cleanupState.eventStreamController?.stop());

refreshCleanup().then(setupCleanupEvents).catch((error) => {
  document.getElementById("cleanup-status").textContent = `Cleanup unavailable: ${String(error?.message || error)}`;
});
