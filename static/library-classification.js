const state = {
  payload: null,
  classificationId: null,
  docsPage: 1,
  docsPageSize: 40,
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  soundNotifier: null,
};

async function api(path, options = {}) {
  return window.ManzaraCore.api(path, options);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
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
  document.getElementById("global-status").textContent = `Tasks: ${active} • Flows: ${activeWorkflows}`;
  const stopBtn = document.getElementById("stop-all-btn");
  window.ManzaraCore.applyStopAllButton(stopBtn, payload.global.stop_all_state);
}

function renderLanguageDistribution(items) {
  if (!items || !items.length) {
    return '<div class="workflow-footnote">No language stats.</div>';
  }
  const total = items.reduce((acc, item) => acc + Number(item.count || 0), 0);
  return items
    .map((item) => {
      const count = Number(item.count || 0);
      const share = total > 0 ? Math.round((count / total) * 10000) / 100 : 0;
      return `
        <div class="distribution-row">
          <div class="distribution-head">
            <span>${escapeHtml(item.language || "-")}</span>
            <span>${count} • ${share}%</span>
          </div>
          <div class="distribution-bar"><span style="width:${Math.max(0, Math.min(100, share))}%"></span></div>
        </div>
      `;
    })
    .join("");
}

function renderMetaRuns(items) {
  if (!items || !items.length) {
    return '<div class="run-row">No recent meta evaluate runs.</div>';
  }
  return items
    .map(
      (run) => `
      <div class="run-row">
        <div>#${run.run_id} • ${escapeHtml(run.status || "-")}</div>
        <div>${escapeHtml(formatDateTime(run.started_at))}</div>
      </div>
    `
    )
    .join("");
}

function renderDetail(payload) {
  state.payload = payload;
  const detail = payload.detail || {};
  const classification = detail.classification;
  const statusNode = document.getElementById("classification-status");

  if (!detail.available || !classification) {
    document.getElementById("classification-title").textContent = "Classification";
    statusNode.textContent = `Classification unavailable: ${detail.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
    document.getElementById("classification-stat-grid").innerHTML = "";
    document.getElementById("linked-docs-body").innerHTML = "";
    document.getElementById("language-root").innerHTML = "";
    document.getElementById("meta-runs-root").innerHTML = renderMetaRuns(payload.recent_meta_evaluate_runs || []);
    return;
  }

  statusNode.classList.remove("library-status-error");
  statusNode.textContent = `Loaded from ${escapeHtml(detail.config_source || "-")}`;
  document.getElementById("classification-title").textContent = `Classification ${classification.ddc || "#"}`;
  document.getElementById("classification-stat-grid").innerHTML = `
    <div class="library-stat-card"><span class="library-stat-label">ID</span><span class="library-stat-value">${classification.classification_id}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">DDC</span><span class="library-stat-value">${escapeHtml(classification.ddc || "-")}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Usage</span><span class="library-stat-value">${classification.usage_count || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Status</span><span class="library-stat-value">${escapeHtml(classification.status || "-")}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Path (EN)</span><span class="library-stat-value">${escapeHtml(classification.path || "-")}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Path (TT)</span><span class="library-stat-value">${escapeHtml(classification.path_tt || "-")}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Created By</span><span class="library-stat-value">${escapeHtml(classification.created_by || "-")}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Created At</span><span class="library-stat-value">${escapeHtml(formatDateTime(classification.created_at))}</span></div>
  `;

  const docs = detail.linked_docs || { items: [], page: 1, total_pages: 1 };
  document.getElementById("linked-docs-body").innerHTML = (docs.items || [])
    .map(
      (item) => `
      <tr>
        <td>${escapeHtml(item.md5 || "-")}</td>
        <td>${escapeHtml(item.language || "-")}</td>
        <td>${escapeHtml(item.mime_type || "-")}</td>
        <td title="${escapeHtml(item.ya_path || "-")}">${escapeHtml(item.ya_path || "-")}</td>
      </tr>
    `
    )
    .join("");
  document.getElementById("docs-page-label").textContent = `Page ${docs.page} / ${docs.total_pages}`;
  document.getElementById("docs-prev").disabled = docs.page <= 1;
  document.getElementById("docs-next").disabled = docs.page >= docs.total_pages;

  document.getElementById("language-root").innerHTML = renderLanguageDistribution(
    detail.language_distribution || []
  );
  document.getElementById("meta-runs-root").innerHTML = renderMetaRuns(payload.recent_meta_evaluate_runs || []);
  renderGlobalState(payload);
  lucide.createIcons();
}

function classificationIdFromPath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  const id = Number(parts[parts.length - 1] || "0");
  return Number.isFinite(id) && id > 0 ? id : 0;
}

async function refreshDetail() {
  const payload = await api(
    `/api/library/classifications/${encodeURIComponent(String(state.classificationId))}?docs_page=${state.docsPage}&docs_page_size=${state.docsPageSize}`
  );
  renderDetail(payload);
}

function queueRefresh(delayMs = 250) {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(async () => {
    state.refreshTimer = null;
    try {
      await refreshDetail();
    } catch (error) {
      console.error(error);
    }
  }, delayMs);
}

async function stopAll() {
  const stopState = state.payload?.global?.stop_all_state;
  if (stopState === "armed") {
    const confirmed = window.confirm("Force stop all running tasks immediately?");
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
      queueRefresh(150);
    },
  });
  state.eventStreamController.start();
}

function attachUiHandlers() {

  document.getElementById("stop-all-btn").addEventListener("click", () => {
    stopAll().catch((error) => console.error(error));
  });

  document.getElementById("docs-prev").addEventListener("click", () => {
    const page = state.payload?.detail?.linked_docs?.page || 1;
    if (page <= 1) return;
    state.docsPage = page - 1;
    queueRefresh(0);
  });

  document.getElementById("docs-next").addEventListener("click", () => {
    const page = state.payload?.detail?.linked_docs?.page || 1;
    const total = state.payload?.detail?.linked_docs?.total_pages || 1;
    if (page >= total) return;
    state.docsPage = page + 1;
    queueRefresh(0);
  });
}

async function bootstrap() {
  state.classificationId = classificationIdFromPath();
  if (!state.classificationId) {
    throw new Error("Invalid classification id");
  }

  initSoundNotifier();
  window.addEventListener("beforeunload", () => {
    teardownSoundNotifier();
    if (state.eventStreamController) {
      state.eventStreamController.stop();
      state.eventStreamController = null;
    }

  });
  attachUiHandlers();
  await refreshDetail();
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  alert(error.message || String(error));
});
