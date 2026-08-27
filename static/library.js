const state = {
  payload: null,
  viewState: "loading",
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  soundNotifier: null,
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

async function api(path, options = {}) {
  return window.ManzaraCore.api(path, options);
}

function escapeHtml(value) {
  return window.ManzaraCore.escapeHtml(value);
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
  document.getElementById("global-status").textContent = window.ManzaraCore.formatGlobalStatus(
    active
  );
  const stopBtn = document.getElementById("stop-all-btn");
  window.ManzaraCore.applyStopAllButton(stopBtn, payload.global.stop_all_state);
}

function renderStatGrid(stats) {
  return `
    <div class="library-stat-card"><span class="library-stat-label">Applicable</span><span class="library-stat-value">${stats.applicable_docs || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Non-applicable</span><span class="library-stat-value">${stats.non_applicable_docs || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Pending</span><span class="library-stat-value">${stats.pending_evaluation || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Acceptance Rate</span><span class="library-stat-value">${Number(stats.acceptance_rate || 0).toFixed(2)}%</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Classified</span><span class="library-stat-value">${stats.classified_docs || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Classification Coverage</span><span class="library-stat-value">${Number(stats.classification_coverage || 0).toFixed(2)}%</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Metadata Rows</span><span class="library-stat-value">${stats.metadata_rows || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Total Documents</span><span class="library-stat-value">${stats.total_documents || 0}</span></div>
  `;
}

function renderPreviewGrid(stats) {
  const items = [
    { label: "Eligible PDFs", value: Number(stats.eligible || 0) },
    { label: "Ready", value: Number(stats.ready || 0) },
    { label: "Pending", value: Number(stats.pending || 0) },
    { label: "Partial", value: Number(stats.partial || 0) },
    { label: "Failed", value: Number(stats.failed || 0) },
    { label: "Preview Pages", value: Number(stats.generated_preview_pages || 0) },
    { label: "Image Objects", value: Number(stats.generated_image_objects || 0) },
  ];
  return items
    .map(
      (item) => `
        <div class="library-stat-card">
          <span class="library-stat-label">${escapeHtml(item.label)}</span>
          <span class="library-stat-value">${escapeHtml(String(item.value))}</span>
        </div>
      `
    )
    .join("");
}

function applyPreviewProgressEvent(payload) {
  if (
    String(payload?.type || "") !== "task.progress"
    || String(payload?.task_id || "") !== "library.generate_book_previews"
  ) {
    return false;
  }
  const progress = payload?.payload?.progress;
  if (!progress || typeof progress !== "object") return false;
  const current = Number(progress.current || 0);
  const total = Number(progress.total || 0);
  const ready = Number(progress.ready || 0);
  const partial = Number(progress.partial || 0);
  const failed = Number(progress.failed || 0);
  document.getElementById("library-preview-status").textContent =
    `Generating ${current} / ${total} · ${ready} ready · ${partial} partial · ${failed} failed`;
  return true;
}

function renderTopClassifications(items) {
  if (!items || !items.length) {
    return '<div class="run-row">No classifications yet.</div>';
  }
  return items
    .map((item) => {
      const classificationId = Number(item.classification_id || 0);
      const ddcHtml =
        classificationId > 0
          ? `<a class="library-top-ddc" href="/library/classifications/${encodeURIComponent(String(classificationId))}">${escapeHtml(item.ddc || "-")}</a>`
          : `<span class="library-top-ddc">${escapeHtml(item.ddc || "-")}</span>`;
      return `
      <div class="library-top-row">
        <div class="library-top-main">
          ${ddcHtml}
          <span class="library-top-path">${escapeHtml(item.path || "-")}</span>
        </div>
        <span class="library-top-count">${escapeHtml(String(item.usage_count || 0))}</span>
      </div>
    `
    })
    .join("");
}

function renderLastRun(run) {
  if (!run) {
    return '<div class="run-row">No run recorded yet.</div>';
  }
  return `
    <div class="run-result-grid">
      <div><span class="meta-k">Status</span><span class="meta-v">${escapeHtml(run.status || "-")}</span></div>
      <div><span class="meta-k">Run ID</span><span class="meta-v">${escapeHtml(String(run.run_id || "-"))}</span></div>
      <div><span class="meta-k">Started</span><span class="meta-v">${escapeHtml(formatDateTime(run.started_at))}</span></div>
      <div><span class="meta-k">Finished</span><span class="meta-v">${escapeHtml(formatDateTime(run.finished_at))}</span></div>
      <div><span class="meta-k">Exit Code</span><span class="meta-v">${escapeHtml(String(run.exit_code ?? "-"))}</span></div>
    </div>
    ${
      run.error_text
        ? `<div class="run-error-box">${escapeHtml(run.error_text)}</div>`
        : '<div class="workflow-footnote">No error text for last run.</div>'
    }
  `;
}

function renderLibrary(payload) {
  viewState.set("ready");
  state.payload = payload;
  const dataset = payload.dataset || {};
  const stats = dataset.stats || {};
  const statusNode = document.getElementById("library-status");
  if (dataset.available) {
    const source = dataset.config_source ? ` • source: ${dataset.config_source}` : "";
    statusNode.textContent = `Dataset stats loaded${source}`;
    statusNode.classList.remove("library-status-error");
  } else {
    statusNode.textContent = `Dataset unavailable: ${dataset.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
  }

  document.getElementById("library-stat-grid").innerHTML = renderStatGrid(stats);
  const previewStats = dataset.preview_stats || {};
  const recipeVersion = String(previewStats.recipe_version || "current recipe");
  document.getElementById("library-preview-status").textContent =
    `Coverage for ${recipeVersion}`;
  document.getElementById("library-preview-grid").innerHTML = renderPreviewGrid(previewStats);
  document.getElementById("library-top-list").innerHTML = renderTopClassifications(
    dataset.top_classifications || []
  );
  document.getElementById("library-last-run").innerHTML = renderLastRun(payload.last_eval_run);
  renderGlobalState(payload);
  lucide.createIcons();
}

function renderLibraryLoading() {
  viewState.set("loading");
  const statusNode = document.getElementById("library-status");
  statusNode.textContent = "Loading library dataset...";
  statusNode.classList.remove("library-status-error");
  document.getElementById("library-stat-grid").innerHTML = '<div class="run-row">Loading stats...</div>';
  document.getElementById("library-top-list").innerHTML = '<div class="run-row">Loading classifications...</div>';
  document.getElementById("library-last-run").innerHTML = '<div class="run-row">Loading last run...</div>';
  document.getElementById("library-preview-status").textContent = "Loading preview coverage...";
  document.getElementById("library-preview-grid").innerHTML = '<div class="run-row">Loading previews...</div>';
}

function renderLibraryError(error) {
  viewState.set("error");
  const message = String(error?.message || error || "Failed to load library.");
  const safe = escapeHtml(message);
  const statusNode = document.getElementById("library-status");
  statusNode.textContent = `Library unavailable: ${message}`;
  statusNode.classList.add("library-status-error");
  document.getElementById("library-stat-grid").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
  document.getElementById("library-top-list").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
  document.getElementById("library-last-run").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
  document.getElementById("library-preview-status").textContent = `Preview coverage unavailable: ${message}`;
  document.getElementById("library-preview-grid").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
}

async function refreshLibrary({ showLoading = false } = {}) {
  if (showLoading) {
    renderLibraryLoading();
  }
  try {
    const payload = await api("/api/library");
    renderLibrary(payload);
  } catch (error) {
    renderLibraryError(error);
    throw error;
  }
}

function queueRefresh(delayMs = 250) {
  window.ManzaraCore.scheduleRefresh(state, refreshLibrary, delayMs);
}

async function stopAll() {
  const stopState = state.payload?.global?.stop_all_state;
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
    initialCursor: window.ManzaraCore.eventCursorFromSnapshot(state.payload),
    getCursor: () => Number(state.eventCursor || 0),
    setCursor: (nextCursor) => {
      state.eventCursor = Number(nextCursor || 0);
    },
    onEvent: (payload, event) => {
      document.getElementById("last-event").textContent = window.ManzaraCore.formatEventBanner(payload);
      maybePlayTaskNotification(payload, event.lastEventId || "");
      applyPreviewProgressEvent(payload);
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

  document.getElementById("stop-all-btn").addEventListener("click", () => {
    stopAll().catch((error) => console.error(error));
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
  await refreshLibrary({ showLoading: true });
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  window.ManzaraUI.toast(error.message || String(error), { tone: "error" });
});
