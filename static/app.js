const state = {
  dashboard: null,
  viewState: "loading",
  refreshTimer: null,
  logViewer: null,
  eventCursor: 0,
  eventStreamController: null,
  editMode: null,
  editId: null,
  editValue: "",
  pendingRefresh: false,
  soundNotifier: null,
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

async function api(path, options = {}) {
  return window.ManzaraCore.api(path, options);
}

function formatDateTime(value) {
  return window.ManzaraCore.formatDateTime(value);
}

function escapeHtml(value) {
  return window.ManzaraCore.escapeHtml(value);
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
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

function cssName(name, fallback = "unknown") {
  return window.ManzaraCore.cssName(name, fallback);
}

function taskControlModel(task) {
  const status = task.run?.status || "idle";
  if (status === "stopping_graceful") {
    return {
      icon: "octagon-x",
      title: "Force stop now",
      btnClass: "red",
      disabled: false,
      showProgress: true,
      progressClass: "progress-force",
      label: "Stopping (graceful requested)",
    };
  }

  if (status === "stopping_force") {
    return {
      icon: "octagon-x",
      title: "Force stopping",
      btnClass: "red",
      disabled: true,
      showProgress: true,
      progressClass: "progress-force",
      label: "Force stopping",
    };
  }

  if (status === "starting" || status === "running") {
    return {
      icon: "square",
      title: "Request graceful stop",
      btnClass: "active",
      disabled: false,
      showProgress: true,
      progressClass: "",
      label: status === "starting" ? "Starting" : "Running",
    };
  }

  return {
    icon: window.ManzaraCore.toLucideIcon(task.icon_idle, "play"),
    title: `Start ${task.title}`,
    btnClass: "",
    disabled: false,
    showProgress: false,
    progressClass: "",
    label: {
      idle: "Idle",
      completed: "Completed",
      failed: "Failed",
      stopped: "Stopped",
    }[status] || status,
  };
}

function isActiveStatus(status) {
  return (
    status === "starting" ||
    status === "running" ||
    status === "stopping_graceful" ||
    status === "stopping_force"
  );
}

function panelHealth(panel) {
  const counts = panel.status_counts || {};
  const active =
    (counts.starting || 0) +
    (counts.running || 0) +
    (counts.stopping_graceful || 0) +
    (counts.stopping_force || 0);
  const failed = counts.failed || 0;

  if (active > 0) {
    return {
      label: "Running",
      className: "state-running",
      active,
      failed,
    };
  }
  if (failed > 0) {
    return {
      label: "Attention",
      className: "state-attention",
      active,
      failed,
    };
  }
  return {
    label: "Healthy",
    className: "state-healthy",
    active,
    failed,
  };
}

function findTaskTitle(taskId) {
  for (const panel of state.dashboard?.panels || []) {
    for (const task of panel.tasks || []) {
      if (task.task_id === taskId) return task.title || "";
    }
  }
  return "";
}

function findFlowTitle(panelId) {
  for (const panel of state.dashboard?.panels || []) {
    if (panel.panel_id === panelId) return panel.title || "";
  }
  return "";
}

function focusInlineInput() {
  if (!state.editMode || !state.editId) return;
  const selector = `.inline-edit-input[data-edit-kind="${state.editMode}"][data-edit-id="${state.editId}"]`;
  const input = document.querySelector(selector);
  if (input instanceof HTMLInputElement) {
    input.focus();
    input.select();
  }
}

function startInlineEdit(kind, id, currentTitle) {
  state.editMode = kind;
  state.editId = id;
  state.editValue = String(currentTitle || "");
  if (state.dashboard) {
    renderDashboard(state.dashboard);
    requestAnimationFrame(focusInlineInput);
  }
}

function resumePendingRefresh() {
  if (state.pendingRefresh) {
    state.pendingRefresh = false;
    queueRefresh(0);
  }
}

function cancelInlineEdit() {
  if (!state.editMode) return;
  state.editMode = null;
  state.editId = null;
  state.editValue = "";
  if (state.dashboard) {
    renderDashboard(state.dashboard);
  }
  resumePendingRefresh();
}

async function renameTask(taskId, title) {
  await api(`/api/tasks/${encodeURIComponent(taskId)}/title`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

async function renameFlow(panelId, title) {
  await api(`/api/flows/${encodeURIComponent(panelId)}/title`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

async function saveInlineEdit() {
  if (!state.editMode || !state.editId) return;

  const mode = state.editMode;
  const id = state.editId;
  const title = state.editValue.trim();
  if (!title) {
    cancelInlineEdit();
    return;
  }

  const currentTitle = mode === "task" ? findTaskTitle(id) : findFlowTitle(id);
  if (title === currentTitle) {
    cancelInlineEdit();
    return;
  }

  if (mode === "task") {
    await renameTask(id, title);
  } else {
    await renameFlow(id, title);
  }

  state.editMode = null;
  state.editId = null;
  state.editValue = "";
  state.pendingRefresh = false;
  queueRefresh(0);
}

function renderPanel(panel) {
  const health = panelHealth(panel);
  const isShayan = panel.panel_id === "shayan";
  let workflowHtml = "";
  if (isShayan) {
    const scanActive = panel.tasks.some(
      (task) => task.task_type === "scan" && isActiveStatus(task.run?.status || "idle")
    );
    const downloadActive = panel.tasks.some(
      (task) =>
        task.task_type === "download" && isActiveStatus(task.run?.status || "idle")
    );

    workflowHtml = `
      <div class="workflow">
        <div class="workflow-step ${scanActive ? "active" : ""}">
          <span class="step-dot"></span>
          <span class="step-label">Catalog Scan</span>
        </div>
        <div class="workflow-step ${downloadActive ? "active" : ""}">
          <span class="step-dot"></span>
          <span class="step-label">Download Sync</span>
        </div>
        <div class="workflow-step">
          <span class="step-dot"></span>
          <span class="step-label">Archive Review</span>
        </div>
        <div class="workflow-step">
          <span class="step-dot"></span>
          <span class="step-label">Distribution</span>
        </div>
      </div>
    `;
  }

  const tasksHtml = panel.tasks
    .map((task) => {
      const taskPathKey = encodeURIComponent(task.slug || task.task_id);
      const model = taskControlModel(task);
      const runStatus = task.run?.status || "idle";
      const runId = task.run?.run_id || "";
      const logsDisabled = !runId;
      const statusText = model.label;
      const taskClass = `task-card task-type-${cssName(task.task_type, "generic")} task-status-${cssName(runStatus, "idle")}`;
      const isTaskEditing = state.editMode === "task" && state.editId === task.task_id;
      const taskTitleHtml = isTaskEditing
        ? `
          <div class="inline-edit">
            <input
              class="inline-edit-input"
              data-edit-kind="task"
              data-edit-id="${escapeAttr(task.task_id)}"
              value="${escapeAttr(state.editValue)}"
              maxlength="80"
            />
          </div>
        `
        : `
          <div class="task-title-row">
            <a class="task-title task-detail-link" href="/tasks/${taskPathKey}">${escapeHtml(task.title)}</a>
            <button
              class="icon-btn rename-inline-btn task-rename-start"
              data-task-id="${escapeAttr(task.task_id)}"
              data-task-title="${escapeAttr(task.title)}"
              title="Rename task"
              aria-label="Rename task"
            >
              <i data-lucide="pencil"></i>
            </button>
          </div>
        `;
      const progress = task.run?.progress && typeof task.run.progress === "object"
        ? task.run.progress
        : {};
      const progressCurrent = Number(progress.current);
      const progressTotal = Number(progress.total);
      const hasDeterminateProgress = model.showProgress
        && Number.isFinite(progressCurrent)
        && progressCurrent >= 0
        && Number.isFinite(progressTotal)
        && progressTotal > 0;
      const suppliedPercent = Number(progress.percent);
      const progressPercent = Math.max(0, Math.min(
        100,
        Number.isFinite(suppliedPercent)
          ? suppliedPercent
          : hasDeterminateProgress ? (progressCurrent / progressTotal) * 100 : 0,
      ));
      const progressHtml = model.showProgress
        ? hasDeterminateProgress
          ? `
            <div class="progress-meta">
              <span>${escapeHtml(String(progressCurrent))} / ${escapeHtml(String(progressTotal))}</span>
              <span>${escapeHtml(String(Math.round(progressPercent)))}%</span>
            </div>
            <div class="progress-wrap" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${escapeHtml(String(Math.round(progressPercent)))}">
              <div class="progress-determinate ${model.progressClass}" style="width: ${progressPercent}%"></div>
            </div>`
          : `<div class="progress-wrap"><div class="progress-indeterminate ${model.progressClass}"></div></div>`
        : "";

      return `
        <div class="${taskClass}">
          <div class="task-card-head">
            <div class="task-type-chip">${escapeHtml(task.task_type)}</div>
            ${taskTitleHtml}
            ${window.ManzaraCore.renderTaskStatusBadge(task.run || {}, { label: statusText })}
          </div>
          ${progressHtml}
          <div class="task-controls-grid">
            <button
              class="icon-btn task-main-btn task-toggle ${model.btnClass}"
              data-task-id="${escapeHtml(task.task_id)}"
              title="${escapeHtml(model.title)}"
              aria-label="${escapeHtml(model.title)}"
              ${model.disabled ? "disabled" : ""}
            >
              <i data-lucide="${model.icon}"></i>
            </button>
            <button
              class="icon-btn task-log-btn task-logs"
              data-run-id="${runId}"
              data-task-title="${escapeHtml(task.title)}"
              title="Show logs"
              aria-label="Show logs"
              ${logsDisabled ? "disabled" : ""}
            >
              <i data-lucide="terminal"></i>
            </button>
            <a
              class="icon-btn task-detail-btn"
              href="/tasks/${taskPathKey}"
              title="Task details"
              aria-label="Task details"
            >
              <i data-lucide="panel-right-open"></i>
            </a>
          </div>
        </div>
      `;
    })
    .join("");

  const statsHtml = isShayan
    ? `
      <div class="stat"><div class="stat-label">Downloaded Files</div><div class="stat-value">${panel.stats.downloaded_files_total}</div></div>
      <div class="stat"><div class="stat-label">New Last Run</div><div class="stat-value">${panel.stats.newly_downloaded_last_run}</div></div>
      <div class="stat"><div class="stat-label">Failed Last Run</div><div class="stat-value">${panel.stats.failed_last_run}</div></div>
      <div class="stat"><div class="stat-label">Last Success</div><div class="stat-value">${escapeHtml(formatDateTime(panel.stats.last_successful_run))}</div></div>
    `
    : (panel.stats_cards || [])
        .map((item) => {
          let value = item.value;
          if (
            typeof value === "string" &&
            value.includes("T") &&
            !Number.isNaN(Date.parse(value))
          ) {
            value = formatDateTime(value);
          }
          return `<div class="stat"><div class="stat-label">${escapeHtml(item.label)}</div><div class="stat-value">${escapeHtml(String(value ?? "-"))}</div></div>`;
        })
        .join("");

  const isFlowEditing = state.editMode === "flow" && state.editId === panel.panel_id;
  const panelTitleHtml = isFlowEditing
    ? `
      <div class="inline-edit panel-inline-edit">
        <input
          class="inline-edit-input"
          data-edit-kind="flow"
          data-edit-id="${escapeAttr(panel.panel_id)}"
          value="${escapeAttr(state.editValue)}"
          maxlength="80"
        />
      </div>
    `
    : `
      <div class="panel-title-row">
        <h2>${escapeHtml(panel.title)}</h2>
        <button
          class="icon-btn rename-inline-btn flow-rename-start"
          data-panel-id="${escapeAttr(panel.panel_id)}"
          data-panel-title="${escapeAttr(panel.title)}"
          title="Rename flow"
          aria-label="Rename flow"
        >
          <i data-lucide="pencil"></i>
        </button>
      </div>
    `;

  return `
    <section class="panel">
      <div class="panel-head">
        <div class="panel-head-left">
          ${panelTitleHtml}
          ${panel.description ? `<div class="subtitle-lite">${escapeHtml(panel.description)}</div>` : ""}
        </div>
        <div class="panel-head-right">
          <span class="panel-pill ${health.className}">${health.label}</span>
          <span class="panel-pill">Active ${health.active}</span>
          <span class="panel-pill">Failed ${health.failed}</span>
        </div>
      </div>
      ${workflowHtml}
      <div class="stats">
        ${statsHtml}
      </div>
      <div class="tasks">${tasksHtml}</div>
    </section>
  `;
}

function renderRuns(runs) {
  if (!runs.length) {
    return '<div class="run-row">No runs yet.</div>';
  }
  return runs
    .map(
      (run) => `
      <div class="run-row">
        <div><a class="run-task-link" href="/tasks/${encodeURIComponent(run.task_slug || run.task_id)}">${escapeHtml(run.task_id)}</a> • ${escapeHtml(run.status)}</div>
        <div>${escapeHtml(formatDateTime(run.started_at))}</div>
      </div>
    `
    )
    .join("");
}

function renderGlobalState(payload) {
  const active = payload.global.active_tasks || 0;
  document.getElementById("global-status").textContent = window.ManzaraCore.formatGlobalStatus(
    active
  );
  const stopBtn = document.getElementById("stop-all-btn");
  window.ManzaraCore.applyStopAllButton(stopBtn, payload.global.stop_all_state);
}

function renderDashboard(payload) {
  viewState.set("ready");
  state.dashboard = payload;
  const panelGrid = document.getElementById("panel-grid");
  const panels = Array.isArray(payload.panels) ? payload.panels : [];
  if (!panels.length) {
    viewState.set("empty");
    panelGrid.innerHTML = '<div class="run-row">No flows available yet.</div>';
  } else {
    panelGrid.innerHTML = panels.map(renderPanel).join("");
  }
  document.getElementById("runs-list").innerHTML = renderRuns(payload.recent_runs || []);
  renderGlobalState(payload);
  lucide.createIcons();
}

function renderDashboardLoading() {
  viewState.set("loading");
  document.getElementById("panel-grid").innerHTML = '<div class="run-row">Loading flows...</div>';
  document.getElementById("runs-list").innerHTML = '<div class="run-row">Loading runs...</div>';
}

function renderDashboardError(error) {
  viewState.set("error");
  const message = String(error?.message || error || "Failed to load dashboard.");
  const safe = escapeHtml(message);
  document.getElementById("panel-grid").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
  document.getElementById("runs-list").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
}

async function refreshDashboard({ showLoading = false } = {}) {
  if (showLoading) {
    renderDashboardLoading();
  }
  try {
    const payload = await api("/api/dashboard");
    renderDashboard(payload);
  } catch (error) {
    renderDashboardError(error);
    throw error;
  }
}

function queueRefresh(delayMs = 250) {
  if (state.editMode) {
    state.pendingRefresh = true;
    return;
  }
  window.ManzaraCore.scheduleRefresh(state, refreshDashboard, delayMs);
}

async function toggleTask(taskId) {
  const result = await api(`/api/tasks/${encodeURIComponent(taskId)}/toggle`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  maybeShowTaskActionError(result);
  queueRefresh(0);
}

async function stopAll() {
  const stopState = state.dashboard?.global?.stop_all_state;
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
    dialogNode: document.getElementById("log-dialog"),
    titleNode: document.getElementById("log-title"),
    contentNode: document.getElementById("log-content"),
    tailLimit: 400,
    followLimit: 400,
    backfillLimit: 400,
    pollIntervalMs: 1500,
  });
}

function setupEventStream() {
  state.eventStreamController?.stop();
  state.eventStreamController = window.ManzaraCore.createSseController({
    eventTypes: window.ManzaraCore.DEFAULT_EVENT_TYPES,
    initialCursor: window.ManzaraCore.eventCursorFromSnapshot(state.dashboard),
    getCursor: () => Number(state.eventCursor || 0),
    setCursor: (nextCursor) => {
      state.eventCursor = Number(nextCursor || 0);
    },
    onEvent: (payload, event) => {
      document.getElementById("last-event").textContent = window.ManzaraCore.formatEventBanner(payload);
      maybePlayTaskNotification(payload, event.lastEventId || "");
      if (window.ManzaraCore.applyTaskEventState(state.dashboard, payload)) {
        renderDashboard(state.dashboard);
      }
      if (window.ManzaraCore.eventNeedsReconciliation(payload)) {
        queueRefresh(100);
      }
    },
  });
  state.eventStreamController.start();
}

function attachUiHandlers() {

  document.getElementById("panel-grid").addEventListener("click", (event) => {
    const target = event.target.closest("button");
    if (!target) return;

    if (target.classList.contains("task-toggle")) {
      const taskId = target.dataset.taskId;
      if (taskId) {
        toggleTask(taskId).catch((error) => console.error(error));
      }
      return;
    }

    if (target.classList.contains("task-rename-start")) {
      const taskId = target.dataset.taskId;
      const taskTitle = target.dataset.taskTitle || "";
      if (taskId) {
        startInlineEdit("task", taskId, taskTitle);
      }
      return;
    }

    if (target.classList.contains("flow-rename-start")) {
      const panelId = target.dataset.panelId;
      const panelTitle = target.dataset.panelTitle || "";
      if (panelId) {
        startInlineEdit("flow", panelId, panelTitle);
      }
      return;
    }

    if (target.classList.contains("task-logs")) {
      const runId = Number(target.dataset.runId || "0");
      const taskTitle = target.dataset.taskTitle || "Task";
      if (runId > 0) {
        state.logViewer?.open(runId, taskTitle).catch((error) => console.error(error));
      }
    }
  });

  document.getElementById("panel-grid").addEventListener("input", (event) => {
    const input = event.target.closest(".inline-edit-input");
    if (!input) return;
    const editKind = input.dataset.editKind || null;
    const editId = input.dataset.editId || null;
    if (editKind !== state.editMode || editId !== state.editId) return;
    state.editValue = input.value;
  });

  document.getElementById("panel-grid").addEventListener("keydown", (event) => {
    const input = event.target.closest(".inline-edit-input");
    if (!input) return;
    if (event.key === "Enter") {
      event.preventDefault();
      saveInlineEdit().catch((error) => console.error(error));
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      cancelInlineEdit();
    }
  });

  document.getElementById("panel-grid").addEventListener("focusout", (event) => {
    const input = event.target.closest(".inline-edit-input");
    if (!input) return;
    if (!state.editMode || !state.editId) return;
    setTimeout(() => {
      if (state.editMode) {
        saveInlineEdit().catch((error) => console.error(error));
      }
    }, 0);
  });

  document.getElementById("stop-all-btn").addEventListener("click", () => {
    stopAll().catch((error) => console.error(error));
  });

  document.getElementById("close-logs").addEventListener("click", () => {
    state.logViewer?.close();
  });
  document.getElementById("log-dialog").addEventListener("close", () => {
    state.logViewer?.close({ closeDialog: false });
  });

  document.getElementById("copy-logs").addEventListener("click", async () => {
    const text = document.getElementById("log-content").textContent;
    await navigator.clipboard.writeText(text || "");
  });
}

async function bootstrap() {
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
  await refreshDashboard({ showLoading: true });
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
});
