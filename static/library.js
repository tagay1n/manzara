const state = {
  payload: null,
  refreshTimer: null,
  eventStream: null,
  eventStreamReconnectTimer: null,
  eventCursor: 0,
  soundNotifier: null,
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString();
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
  stopBtn.disabled = payload.global.stop_all_state === "disabled";
  stopBtn.classList.remove("amber", "red");
  if (payload.global.stop_all_state === "armed") {
    stopBtn.classList.add("red");
    stopBtn.title = "Force stop all running tasks";
    stopBtn.setAttribute("aria-label", "Force stop all running tasks");
    stopBtn.innerHTML = '<i data-lucide="octagon-x"></i>';
  } else {
    stopBtn.classList.add("amber");
    stopBtn.title = "Graceful stop all running tasks";
    stopBtn.setAttribute("aria-label", "Graceful stop all running tasks");
    stopBtn.innerHTML = '<i data-lucide="square"></i>';
  }
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

function renderTopClassifications(items) {
  if (!items || !items.length) {
    return '<div class="run-row">No classifications yet.</div>';
  }
  return items
    .map(
      (item) => `
      <div class="library-top-row">
        <div class="library-top-main">
          <span class="library-top-ddc">${escapeHtml(item.ddc || "-")}</span>
          <span class="library-top-path">${escapeHtml(item.path || "-")}</span>
        </div>
        <span class="library-top-count">${escapeHtml(String(item.usage_count || 0))}</span>
      </div>
    `
    )
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
  document.getElementById("library-top-list").innerHTML = renderTopClassifications(
    dataset.top_classifications || []
  );
  document.getElementById("library-last-run").innerHTML = renderLastRun(payload.last_eval_run);
  renderGlobalState(payload);
  lucide.createIcons();
}

async function refreshLibrary() {
  const payload = await api("/api/library");
  renderLibrary(payload);
}

function queueRefresh(delayMs = 250) {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(async () => {
    state.refreshTimer = null;
    try {
      await refreshLibrary();
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

function scheduleEventStreamReconnect() {
  if (state.eventStreamReconnectTimer) return;
  state.eventStreamReconnectTimer = setTimeout(() => {
    state.eventStreamReconnectTimer = null;
    setupEventStream();
  }, 1500);
}

function streamUrl() {
  const cursor = Number(state.eventCursor || 0);
  if (Number.isFinite(cursor) && cursor > 0) {
    return `/api/events/stream?after_event_id=${encodeURIComponent(String(cursor))}`;
  }
  return "/api/events/stream";
}

function updateEventCursor(event, payload) {
  const fromSse = Number(String(event?.lastEventId || ""));
  const fromPayload = Number(payload?.event_id || 0);
  const candidate = Number.isFinite(fromSse) && fromSse > 0 ? fromSse : fromPayload;
  if (Number.isFinite(candidate) && candidate > Number(state.eventCursor || 0)) {
    state.eventCursor = candidate;
  }
}

function setupEventStream() {
  if (state.eventStream) {
    state.eventStream.close();
    state.eventStream = null;
  }
  const stream = new EventSource(streamUrl());
  state.eventStream = stream;
  const eventTypes = [
    "task.started",
    "task.progress",
    "task.log",
    "task.stop_requested",
    "task.force_stop_requested",
    "task.stopped",
    "task.completed",
    "task.failed",
    "workflow.started",
    "workflow.step_started",
    "workflow.step_completed",
    "workflow.step_skipped",
    "workflow.stopped",
    "workflow.completed",
    "workflow.failed",
    "task.renamed",
    "flow.renamed",
    "schedule.triggered",
    "schedule.updated",
    "schedule.skipped",
    "schedule.skipped_overlap",
    "system.stop_all_requested",
    "system.workflow_recovery",
  ];

  eventTypes.forEach((name) => {
    stream.addEventListener(name, (event) => {
      try {
        const payload = JSON.parse(event.data);
        updateEventCursor(event, payload);
        document.getElementById("last-event").textContent = `Last event: ${payload.type} @ ${new Date(payload.ts).toLocaleTimeString()}`;
        maybePlayTaskNotification(payload, event.lastEventId || "");
      } catch (_error) {
        // ignore malformed events
      }
      queueRefresh(100);
    });
  });

  stream.onopen = () => {
    if (state.eventStreamReconnectTimer) {
      clearTimeout(state.eventStreamReconnectTimer);
      state.eventStreamReconnectTimer = null;
    }
  };

  stream.onerror = () => {
    if (state.eventStream === stream) {
      stream.close();
      state.eventStream = null;
      scheduleEventStreamReconnect();
    }
  };
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
    if (state.eventStreamReconnectTimer) {
      clearTimeout(state.eventStreamReconnectTimer);
      state.eventStreamReconnectTimer = null;
    }
    if (state.eventStream) {
      state.eventStream.close();
      state.eventStream = null;
    }
  });
  attachUiHandlers();
  await refreshLibrary();
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  alert(error.message || String(error));
});
