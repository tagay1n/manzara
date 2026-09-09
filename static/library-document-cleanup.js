const cleanupState = {
  overview: null,
  list: null,
  eventCursor: 0,
  eventStreamController: null,
};

const cleanupApi = (path, options = {}) => window.ManzaraCore.api(path, options);
const cleanupEscape = (value) => window.ManzaraCore.escapeHtml(value);

function cleanupReviewCard(item) {
  const candidates = (item.candidates_json || []).map((candidate) => `
    <label class="collection-proposal-item cleanup-review-item">
      <input type="checkbox" data-review-candidate="${Number(item.review_id)}" value="${cleanupEscape(candidate.md5)}" checked />
      <span class="collection-proposal-item-copy">
        <strong>${cleanupEscape(candidate.title || candidate.md5)}</strong>
        <small>${cleanupEscape([
          candidate.mime_type,
          Number.isInteger(candidate.page_count) && candidate.page_count > 0
            ? `${candidate.page_count} page${candidate.page_count === 1 ? "" : "s"}`
            : "Page count unknown",
          candidate.full ? "Full document" : "Partial document",
        ].filter(Boolean).join(" · "))}</small>
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
    ? `${items.length} ISBN conflict${items.length === 1 ? "" : "s"} to resolve`
    : "No pending ISBN conflicts.";
  document.getElementById("cleanup-list").innerHTML = items.map(cleanupReviewCard).join("")
    || '<div class="collections-review-empty">No pending ISBN conflicts.</div>';
  window.lucide?.createIcons?.();
}

async function refreshCleanup() {
  const overview = await cleanupApi("/api/library/document-cleanup");
  cleanupState.overview = overview;
  cleanupState.eventCursor = Math.max(cleanupState.eventCursor, Number(overview.event_cursor || 0));
  renderCleanupList(await cleanupApi("/api/library/document-cleanup/isbn-reviews?status=pending&limit=500"));
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

document.getElementById("cleanup-list").addEventListener("click", (event) => {
  const action = event.target.closest("[data-review-decide]");
  if (action) decideCleanupReview(Number(action.dataset.reviewDecide)).catch(console.error);
});
window.addEventListener("beforeunload", () => cleanupState.eventStreamController?.stop());

refreshCleanup().then(setupCleanupEvents).catch((error) => {
  document.getElementById("cleanup-status").textContent = `Cleanup unavailable: ${String(error?.message || error)}`;
});
