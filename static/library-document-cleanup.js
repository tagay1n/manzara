const cleanupState = {
  overview: null,
  list: null,
  recent: null,
  eventCursor: 0,
  eventStreamController: null,
  savingReviewIds: new Set(),
  undoingReviewIds: new Set(),
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

function cleanupRecentCard(item) {
  const candidates = item.candidates_json || [];
  const kept = new Set(item.keep_md5s_json || []);
  return `<article class="collection-queue-card cleanup-recent-card">
    <div class="collection-static-row cleanup-review-head">
      <span class="collection-queue-copy">
        <span class="collection-queue-title">ISBN ${cleanupEscape(item.isbn)}</span>
        <span class="collection-queue-meta">Resolved · ${kept.size} of ${candidates.length} kept · ${cleanupEscape(window.ManzaraCore.formatDateTime(item.decided_at))}</span>
      </span>
      <button class="small-btn" data-review-undo="${Number(item.review_id)}">Undo</button>
    </div>
  </article>`;
}

function renderRecentReviews(payload, { reveal = false } = {}) {
  cleanupState.recent = payload;
  const items = payload.items || [];
  const section = document.getElementById("cleanup-recent");
  section.hidden = items.length === 0;
  if (reveal && items.length) section.open = true;
  document.getElementById("cleanup-recent-count").textContent = String(items.length);
  document.getElementById("cleanup-recent-list").innerHTML = items
    .map(cleanupRecentCard)
    .join("");
  window.lucide?.createIcons?.();
}

async function refreshCleanup({ revealRecent = false } = {}) {
  const overview = await cleanupApi("/api/library/document-cleanup");
  cleanupState.overview = overview;
  cleanupState.eventCursor = Math.max(cleanupState.eventCursor, Number(overview.event_cursor || 0));
  const [pending, recent] = await Promise.all([
    cleanupApi("/api/library/document-cleanup/isbn-reviews?status=pending&limit=500"),
    cleanupApi("/api/library/document-cleanup/isbn-reviews?status=decided&limit=20"),
  ]);
  renderCleanupList(pending);
  renderRecentReviews(recent, { reveal: revealRecent });
}

async function decideCleanupReview(reviewId, button) {
  if (cleanupState.savingReviewIds.has(reviewId)) return;
  const selected = [...document.querySelectorAll(`[data-review-candidate="${reviewId}"]:checked`)]
    .map((node) => node.value);
  const all = [...document.querySelectorAll(`[data-review-candidate="${reviewId}"]`)];
  if (!selected.length) {
    window.ManzaraUI.toast("Keep at least one document.", { tone: "warning" });
    return;
  }
  const removed = all.length - selected.length;
  cleanupState.savingReviewIds.add(reviewId);
  if (button) {
    button.disabled = true;
    button.textContent = "Saving...";
  }
  try {
    await cleanupApi(`/api/library/document-cleanup/isbn-reviews/${reviewId}/decision`, {
      method: "POST",
      body: JSON.stringify({ keep_md5s: selected }),
    });
    await refreshCleanup({ revealRecent: true });
    window.ManzaraUI.toast(
      `ISBN resolved: ${selected.length} kept, ${Math.max(0, removed)} queued for cleanup.`
    );
  } finally {
    cleanupState.savingReviewIds.delete(reviewId);
    if (button) {
      button.disabled = false;
      button.textContent = "Keep selected";
    }
  }
}

async function undoCleanupReview(reviewId, button) {
  if (cleanupState.undoingReviewIds.has(reviewId)) return;
  cleanupState.undoingReviewIds.add(reviewId);
  if (button) {
    button.disabled = true;
    button.textContent = "Undoing...";
  }
  try {
    await cleanupApi(`/api/library/document-cleanup/isbn-reviews/${reviewId}/undo`, {
      method: "POST",
    });
    await refreshCleanup();
    window.ManzaraUI.toast("ISBN decision undone. The conflict is active again.");
  } finally {
    cleanupState.undoingReviewIds.delete(reviewId);
    if (button) {
      button.disabled = false;
      button.textContent = "Undo";
    }
  }
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
  if (action) decideCleanupReview(Number(action.dataset.reviewDecide), action).catch((error) => {
    window.ManzaraUI.toast(error.message || String(error), { tone: "error" });
  });
});
document.getElementById("cleanup-recent-list").addEventListener("click", (event) => {
  const action = event.target.closest("[data-review-undo]");
  if (action) undoCleanupReview(Number(action.dataset.reviewUndo), action).catch((error) => {
    window.ManzaraUI.toast(error.message || String(error), { tone: "error" });
  });
});
window.addEventListener("beforeunload", () => cleanupState.eventStreamController?.stop());

refreshCleanup().then(setupCleanupEvents).catch((error) => {
  document.getElementById("cleanup-status").textContent = `Cleanup unavailable: ${String(error?.message || error)}`;
});
