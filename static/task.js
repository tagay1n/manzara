const state = {
  payload: null,
  viewState: "loading",
  taskId: null,
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  selectedRunId: null,
  logViewer: null,
  activeWorkspaceTab: "overview",
  soundNotifier: null,
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

async function api(path, options = {}) {
  return window.ManzaraCore.api(path, options);
}

function escapeHtml(value) {
  return window.ManzaraCore.escapeHtml(value);
}

function cssName(name, fallback = "unknown") {
  return window.ManzaraCore.cssName(name, fallback);
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

function maybeShowTaskActionError(result) {
  window.ManzaraUI?.reportTaskActionResult(result);
}

function isActiveStatus(status) {
  return window.ManzaraCore.isActiveStatus(status);
}

function runDuration(run) {
  if (!run?.started_at) return "-";
  const start = Date.parse(run.started_at);
  if (Number.isNaN(start)) return "-";
  const endValue = run.finished_at ? Date.parse(run.finished_at) : Date.now();
  if (Number.isNaN(endValue)) return "-";
  const seconds = Math.max(0, Math.floor((endValue - start) / 1000));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}

function selectedRun() {
  if (!state.payload?.runs?.length) return null;
  const selected = state.payload.runs.find((run) => run.run_id === state.selectedRunId);
  if (selected) return selected;
  state.selectedRunId = state.payload.runs[0].run_id;
  return state.payload.runs[0];
}

function runSummary(run) {
  const summary = run?.summary;
  return summary && typeof summary === "object" ? summary : {};
}

function runSummaryMessage(run) {
  const summary = runSummary(run);
  const message = String(summary.message || "").trim();
  if (message) return message;
  const status = String(run?.status || "idle");
  if (status === "completed") return "Run completed.";
  if (status === "failed") return String(run?.error_text || "Run failed.");
  if (status === "stopped") return "Run stopped.";
  return `Run ${status}.`;
}


function renderSummaryArtifacts(summary) {
  const artifacts = summary?.artifacts;
  if (!artifacts || typeof artifacts !== "object") return "";
  const text = JSON.stringify(artifacts, null, 2);
  if (!text) return "";
  return `
    <details class="run-artifacts-box" open>
      <summary>Run artifacts</summary>
      <pre>${escapeHtml(text)}</pre>
    </details>
  `;
}

function renderRunProgress(run) {
  if (!isActiveStatus(run?.status)) return "";
  const progress = run?.progress && typeof run.progress === "object" ? run.progress : {};
  const current = Number(progress.current);
  const total = Number(progress.total);
  const determinate = Number.isFinite(current)
    && current >= 0
    && Number.isFinite(total)
    && total > 0;
  if (!determinate) {
    return '<div class="task-run-progress"><div class="progress-wrap"><div class="progress-indeterminate"></div></div></div>';
  }
  const suppliedPercent = Number(progress.percent);
  const percent = Math.max(0, Math.min(
    100,
    Number.isFinite(suppliedPercent) ? suppliedPercent : (current / total) * 100,
  ));
  const roundedPercent = Math.round(percent);
  return `
    <div class="task-run-progress">
      <div class="progress-meta">
        <span>${escapeHtml(String(current))} / ${escapeHtml(String(total))}</span>
        <span>${escapeHtml(String(roundedPercent))}%</span>
      </div>
      <div class="progress-wrap" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${escapeHtml(String(roundedPercent))}" aria-label="Run progress: ${escapeHtml(String(current))} of ${escapeHtml(String(total))}, ${escapeHtml(String(roundedPercent))}%">
        <div class="progress-determinate" style="width: ${percent}%"></div>
      </div>
    </div>
  `;
}

function toggleButtonModel(task, run) {
  const status = run?.status || "idle";
  if (status === "stopping_graceful") {
    return { icon: "octagon-x", title: "Force stop now", cls: "red" };
  }
  if (status === "stopping_force") {
    return { icon: "octagon-x", title: "Force stopping", cls: "red", disabled: true };
  }
  if (status === "starting" || status === "running") {
    return { icon: "square", title: "Request graceful stop", cls: "active" };
  }
  return {
    icon: window.ManzaraCore.toLucideIcon(task.icon_idle, "play"),
    title: `Start ${task.title}`,
    cls: "",
  };
}

function renderGeminiWorkerControl(task) {
  const target = document.getElementById("gemini-worker-control");
  const config = task.gemini_workers;
  if (!config) {
    target.innerHTML = "";
    return;
  }
  const value = config.active ?? config.next_run ?? config.default;
  if (!config.editable) {
    target.innerHTML = `<span class="gemini-worker-badge">${escapeHtml(String(value))} workers</span>`;
    return;
  }
  const options = Array.from({ length: Number(config.max || 0) }, (_, index) => index + 1)
    .map((count) => `<option value="${count}" ${count === Number(value) ? "selected" : ""}>${count}</option>`)
    .join("");
  target.innerHTML = `<label class="gemini-worker-control">Workers <select id="gemini-workers-select">${options}</select></label>`;
}

function renderRunList(runs) {
  if (!runs.length) {
    return '<div class="run-row">No runs yet.</div>';
  }
  return runs
    .map((run) => {
      const activeClass = run.run_id === state.selectedRunId ? "selected" : "";
      return `
        <button class="task-run-row ${activeClass}" data-run-id="${run.run_id}">
          <span class="task-run-id">#${run.run_id}</span>
          ${window.ManzaraCore.renderTaskStatusBadge(run, { compact: true })}
          <span class="task-run-time">${escapeHtml(formatDateTime(run.started_at))}</span>
          <span class="task-run-summary">${escapeHtml(runSummaryMessage(run))}</span>
        </button>
      `;
    })
    .join("");
}

function renderRunResult(run) {
  if (!run) return '<div class="run-row">No run selected.</div>';
  const errorText = (run.error_text || "").trim();
  const summary = runSummary(run);
  const highlights = Array.isArray(summary.highlights) ? summary.highlights : [];
  const summaryRows = highlights
    .filter((item) => item && typeof item === "object")
    .map((item) => {
      const label = String(item.label || "").trim();
      const value = String(item.value || "").trim();
      if (!label || !value) return "";
      return `<div><span class="meta-k">${escapeHtml(label)}</span><span class="meta-v">${escapeHtml(value)}</span></div>`;
    })
    .filter(Boolean)
    .join("");
  return `
    ${renderRunProgress(run)}
    <div class="run-result-grid">
      <div><span class="meta-k">Run</span><span class="meta-v">#${run.run_id}</span></div>
      <div><span class="meta-k">Status</span><span class="meta-v">${escapeHtml(run.status)}</span></div>
      <div><span class="meta-k">Started</span><span class="meta-v">${escapeHtml(formatDateTime(run.started_at))}</span></div>
      <div><span class="meta-k">Finished</span><span class="meta-v">${escapeHtml(formatDateTime(run.finished_at))}</span></div>
      <div><span class="meta-k">Duration</span><span class="meta-v">${escapeHtml(runDuration(run))}</span></div>
      <div><span class="meta-k">Exit Code</span><span class="meta-v">${escapeHtml(String(run.exit_code ?? "-"))}</span></div>
    </div>
    <div class="workflow-footnote">${escapeHtml(runSummaryMessage(run))}</div>
    ${summaryRows ? `<div class="run-result-grid">${summaryRows}</div>` : ""}
    ${renderSummaryArtifacts(summary)}
    ${
      errorText
        ? `<div class="run-error-box">${escapeHtml(errorText)}</div>`
        : '<div class="workflow-footnote">No error text for this run.</div>'
    }
    <div class="run-result-actions">
      <button class="small-btn" id="show-run-logs" data-run-id="${run.run_id}">Show logs</button>
    </div>
  `;
}

function updateLogViewerState(viewerState = {}) {
  const status = String(viewerState.status || "closed");
  const surface = document.getElementById("log-surface");
  const statusNode = document.getElementById("log-viewer-state");
  const emptyNode = document.getElementById("log-empty-state");
  if (surface) {
    surface.dataset.state = status;
    surface.setAttribute("aria-busy", status === "loading" ? "true" : "false");
  }
  if (!statusNode || !emptyNode) return;

  const lineCount = Number(viewerState.bufferedLines || 0);
  const messages = {
    loading: "Loading recent lines…",
    ready: `${lineCount} log ${lineCount === 1 ? "line" : "lines"} • Following live output`,
    empty: "No log lines yet • Waiting for output",
    error: `Could not load logs: ${String(viewerState.error || "Unknown error")}`,
    closed: "Select Logs to follow the selected run.",
  };
  statusNode.textContent = messages[status] || messages.closed;
  emptyNode.hidden = !["loading", "empty", "error"].includes(status);
  emptyNode.setAttribute("aria-hidden", emptyNode.hidden ? "true" : "false");
  if (status === "loading") emptyNode.textContent = "Loading log output…";
  if (status === "empty") emptyNode.textContent = "No log lines yet.";
  if (status === "error") emptyNode.textContent = "Log output is temporarily unavailable.";
}

function applyWorkspaceTab() {
  const isOverview = state.activeWorkspaceTab === "overview";
  const overviewTab = document.getElementById("workspace-tab-overview");
  const logsTab = document.getElementById("workspace-tab-logs");
  const overviewPanel = document.getElementById("run-overview-panel");
  const logsPanel = document.getElementById("run-logs-panel");
  overviewTab.classList.toggle("active", isOverview);
  overviewTab.setAttribute("aria-selected", isOverview ? "true" : "false");
  overviewTab.setAttribute("tabindex", isOverview ? "0" : "-1");
  logsTab.classList.toggle("active", !isOverview);
  logsTab.setAttribute("aria-selected", isOverview ? "false" : "true");
  logsTab.setAttribute("tabindex", isOverview ? "-1" : "0");
  overviewPanel.hidden = !isOverview;
  logsPanel.hidden = isOverview;
}

async function openSelectedRunLogs() {
  const run = selectedRun();
  const runId = Number(run?.run_id || 0);
  const viewerState = state.logViewer?.getState() || {};
  if (!runId || (viewerState.activeRunId === runId && viewerState.status !== "error")) return;
  await state.logViewer?.open(runId, state.payload?.task?.title || state.taskId);
}

function selectWorkspaceTab(tab, { focus = false } = {}) {
  if (tab !== "overview" && tab !== "logs") return;
  if (tab === "logs" && !selectedRun()) return;
  state.activeWorkspaceTab = tab;
  applyWorkspaceTab();
  const tabNode = document.getElementById(`workspace-tab-${tab}`);
  if (focus && typeof tabNode?.focus === "function") tabNode.focus();
  if (tab === "overview") {
    state.logViewer?.close();
    return;
  }
  openSelectedRunLogs().catch((error) => console.error(error));
}

function syncWorkspaceForRun(run) {
  const logsTab = document.getElementById("workspace-tab-logs");
  logsTab.disabled = !Number(run?.run_id || 0);
  if (logsTab.disabled && state.activeWorkspaceTab === "logs") {
    selectWorkspaceTab("overview");
    return;
  }
  applyWorkspaceTab();
  if (state.activeWorkspaceTab === "logs") {
    openSelectedRunLogs().catch((error) => console.error(error));
  }
}


function renderGlobalState(payload) {
  const active = payload.global.active_tasks || 0;
  document.getElementById("global-status").textContent = window.ManzaraCore.formatGlobalStatus(
    active
  );
  const stopBtn = document.getElementById("stop-all-btn");
  window.ManzaraCore.applyStopAllButton(stopBtn, payload.global.stop_all_state);
}

function renderTaskDetail(payload) {
  viewState.set("ready");
  state.payload = payload;
  const task = payload.task;
  const runs = payload.runs || [];
  if (!state.selectedRunId && runs.length) {
    state.selectedRunId = runs[0].run_id;
  }
  const currentRun = selectedRun();
  const buttonModel = toggleButtonModel(task, currentRun);
  ensureCanonicalTaskPath(task.slug || task.task_id);

  document.getElementById("task-title").textContent = task.title;
  document.getElementById("task-subtitle").textContent = `${payload.panel.title} • ${task.task_id} • ${task.task_type}`;
  document.getElementById("task-stat-grid").innerHTML = `
    <div class="stat"><div class="stat-label">Total Runs</div><div class="stat-value">${payload.stats.total_runs}</div></div>
    <div class="stat"><div class="stat-label">Completed</div><div class="stat-value">${payload.stats.status_counts.completed || 0}</div></div>
    <div class="stat"><div class="stat-label">Failed</div><div class="stat-value">${payload.stats.status_counts.failed || 0}</div></div>
    <div class="stat"><div class="stat-label">Last Success</div><div class="stat-value">${escapeHtml(formatDateTime(payload.stats.last_success_at))}</div></div>
  `;
  document.getElementById("task-run-list").innerHTML = renderRunList(runs);
  document.getElementById("run-result").innerHTML = renderRunResult(currentRun);
  syncWorkspaceForRun(currentRun);
  const toggleBtn = document.getElementById("task-toggle-btn");
  renderGeminiWorkerControl(task);
  toggleBtn.classList.remove("active", "red");
  if (buttonModel.cls) toggleBtn.classList.add(buttonModel.cls);
  toggleBtn.title = buttonModel.title;
  toggleBtn.setAttribute("aria-label", buttonModel.title);
  toggleBtn.disabled = Boolean(buttonModel.disabled);
  toggleBtn.innerHTML = `<i data-lucide="${buttonModel.icon}"></i>`;

  renderGlobalState(payload);
  lucide.createIcons();
}

function renderTaskLoading() {
  viewState.set("loading");
  document.getElementById("task-title").textContent = "Loading task...";
  document.getElementById("task-subtitle").textContent = "";
  document.getElementById("task-stat-grid").innerHTML = "";
  document.getElementById("task-run-list").innerHTML = '<div class="run-row">Loading runs...</div>';
  document.getElementById("run-result").innerHTML = '<div class="run-row">Loading run details...</div>';
  syncWorkspaceForRun(null);
}

function renderTaskError(error) {
  viewState.set("error");
  const message = String(error?.message || error || "Failed to load task details.");
  const safe = escapeHtml(message);
  document.getElementById("task-title").textContent = "Task unavailable";
  document.getElementById("task-subtitle").textContent = safe;
  document.getElementById("task-stat-grid").innerHTML = "";
  document.getElementById("task-run-list").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
  document.getElementById("run-result").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
  syncWorkspaceForRun(null);
}

function ensureCanonicalTaskPath(taskPathKey) {
  const canonical = `/tasks/${encodeURIComponent(taskPathKey)}`;
  if (window.location.pathname !== canonical) {
    window.history.replaceState({}, "", canonical);
  }
}

function readTaskIdFromPath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts.length < 2) return null;
  return decodeURIComponent(parts.slice(1).join("/"));
}

async function refreshTaskDetail({ showLoading = false } = {}) {
  if (showLoading) {
    renderTaskLoading();
  }
  try {
    const payload = await api(`/api/tasks/${encodeURIComponent(state.taskId)}?limit=20`);
    renderTaskDetail(payload);
  } catch (error) {
    renderTaskError(error);
    throw error;
  }
}

function queueRefresh(delayMs = 250) {
  window.ManzaraCore.scheduleRefresh(state, refreshTaskDetail, delayMs);
}

function applyOptimisticToggleState() {
  if (!state.payload || !state.payload.task) return;
  const runs = Array.isArray(state.payload.runs) ? state.payload.runs : [];
  state.payload.runs = runs;
  const selected = selectedRun();
  if (!selected) {
    const nowIso = new Date().toISOString();
    const optimisticRun = {
      run_id: null,
      status: "starting",
      started_at: nowIso,
      finished_at: null,
      exit_code: null,
      error_text: null,
      summary: { status: "starting", message: "Starting..." },
    };
    runs.unshift(optimisticRun);
    state.selectedRunId = optimisticRun.run_id;
    return;
  }

  const currentStatus = String(selected.status || "idle");
  if (currentStatus === "starting" || currentStatus === "running") {
    selected.status = "stopping_graceful";
  } else if (currentStatus === "stopping_graceful") {
    selected.status = "stopping_force";
  } else {
    selected.status = "starting";
    selected.started_at = selected.started_at || new Date().toISOString();
    selected.finished_at = null;
    selected.exit_code = null;
    selected.error_text = null;
  }

  if (runs.length > 0 && Number(runs[0]?.run_id || 0) === Number(selected.run_id || 0)) {
    runs[0] = { ...runs[0], ...selected };
  }
}

function applyToggleResult(result) {
  if (!state.payload || !result || typeof result !== "object") return;
  const run = result.run && typeof result.run === "object" ? result.run : null;
  if (!run) return;

  const runs = Array.isArray(state.payload.runs) ? state.payload.runs : [];
  state.payload.runs = runs;
  const runId = Number(run.run_id || 0);
  if (runId > 0) {
    const index = runs.findIndex((item) => Number(item?.run_id || 0) === runId);
    if (index >= 0) {
      runs[index] = { ...runs[index], ...run };
    } else {
      runs.unshift({ ...run });
    }
    state.selectedRunId = runId;
    return;
  }
  if (runs.length > 0) {
    runs[0] = { ...runs[0], ...run };
  }
}

async function toggleTask() {
  const targetTaskId = String(state.payload?.task?.task_id || state.taskId || "").trim();
  if (!targetTaskId) {
    throw new Error("Task id is missing");
  }
  applyOptimisticToggleState();
  if (state.payload) {
    renderTaskDetail(state.payload);
  }
  const result = await api(`/api/tasks/${encodeURIComponent(targetTaskId)}/toggle`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  maybeShowTaskActionError(result);
  applyToggleResult(result);
  if (state.payload) {
    renderTaskDetail(state.payload);
  }
  queueRefresh(0);
}

async function setGeminiWorkers(workers, select) {
  if (select) select.disabled = true;
  try {
    await api(`/api/tasks/${encodeURIComponent(state.payload.task.task_id)}/gemini-workers`, {
      method: "PATCH",
      body: JSON.stringify({ workers: Number(workers) }),
    });
  } finally {
    queueRefresh(0);
  }
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

function initLogViewer() {
  state.logViewer = window.ManzaraCore.createRunLogViewer({
    api,
    titleNode: document.getElementById("log-title"),
    contentNode: document.getElementById("log-content"),
    tailLimit: 400,
    followLimit: 400,
    backfillLimit: 400,
    pollIntervalMs: 1500,
    onStateChange: updateLogViewerState,
  });
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
      const relevant = String(payload?.task_id || "") === String(state.payload?.task?.task_id || "");
      if (relevant && window.ManzaraCore.applyTaskEventState(state.payload, payload)) {
        renderTaskDetail(state.payload);
      }
      if (relevant && window.ManzaraCore.eventNeedsReconciliation(payload)) {
        queueRefresh(100);
      }
    },
  });
  state.eventStreamController.start();
}

function attachUiHandlers() {

  document.getElementById("gemini-worker-control").addEventListener("change", (event) => {
    const select = event.target?.closest?.("#gemini-workers-select");
    if (!select) return;
    setGeminiWorkers(select.value, select).catch((error) => {
      window.ManzaraUI.toast(error?.message || String(error), { tone: "error" });
    });
  });

  document.getElementById("task-toggle-btn").addEventListener("click", () => {
    toggleTask().catch((error) => {
      console.error(error);
      window.ManzaraUI.toast(error?.message || String(error), { tone: "error" });
    });
  });

  document.getElementById("stop-all-btn").addEventListener("click", () => {
    stopAll().catch((error) => console.error(error));
  });

  document.getElementById("task-run-list").addEventListener("click", (event) => {
    const row = event.target.closest(".task-run-row");
    if (!row) return;
    const runId = Number(row.dataset.runId || 0);
    if (!runId) return;
    state.selectedRunId = runId;
    renderTaskDetail(state.payload);
  });

  for (const tab of ["overview", "logs"]) {
    document.getElementById(`workspace-tab-${tab}`).addEventListener("click", () => {
      selectWorkspaceTab(tab);
    });
  }
  document.querySelector?.(".run-workspace-tabs")?.addEventListener("keydown", (event) => {
    const currentTab = event.target?.closest?.("[role='tab']");
    if (!currentTab) return;
    const keys = { ArrowLeft: "overview", ArrowRight: "logs", Home: "overview", End: "logs" };
    const nextTab = keys[event.key];
    if (!nextTab) return;
    event.preventDefault();
    selectWorkspaceTab(nextTab, { focus: true });
  });

  document.getElementById("run-result").addEventListener("click", (event) => {
    const btn = event.target.closest("#show-run-logs");
    if (btn) {
      const runId = Number(btn.dataset.runId || 0);
      if (!runId) return;
      state.selectedRunId = runId;
      selectWorkspaceTab("logs");
      return;
    }
  });
  document.getElementById("copy-logs").addEventListener("click", async () => {
    const text = document.getElementById("log-content").textContent;
    await navigator.clipboard.writeText(text || "");
    window.ManzaraUI.toast("Logs copied.", { tone: "success" });
  });
}

async function bootstrap() {
  state.taskId = readTaskIdFromPath();
  if (!state.taskId) {
    throw new Error("Task id is missing in URL");
  }

  initSoundNotifier();
  initLogViewer();
  window.addEventListener("beforeunload", () => {
    teardownSoundNotifier();
    state.logViewer?.destroy();
    state.logViewer = null;
    if (state.eventStreamController) {
      state.eventStreamController.stop();
      state.eventStreamController = null;
    }

  });
  attachUiHandlers();
  await refreshTaskDetail({ showLoading: true });
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  window.ManzaraUI.toast(error.message || String(error), { tone: "error" });
});
