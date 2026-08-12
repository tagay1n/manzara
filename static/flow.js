const state = {
  payload: null,
  shayanCatalog: null,
  viewState: "loading",
  flowKey: null,
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  logViewer: null,
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
      statusLabel: "Stopping (graceful requested)",
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
      statusLabel: "Force stopping",
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
      statusLabel: status === "starting" ? "Starting" : "Running",
    };
  }

  return {
    icon: window.ManzaraCore.toLucideIcon(task.icon_idle, "play"),
    title: `Start ${task.title}`,
    btnClass: "",
    disabled: false,
    showProgress: false,
    progressClass: "",
    statusLabel: {
      idle: "Idle",
      completed: "Completed",
      failed: "Failed",
      stopped: "Stopped",
    }[status] || status,
  };
}

function runSummaryMessage(run) {
  const summary = run?.summary;
  if (summary && typeof summary === "object") {
    const message = String(summary.message || "").trim();
    if (message) return message;
  }
  const status = String(run?.status || "idle");
  if (status === "completed") return "Run completed.";
  if (status === "failed") return String(run?.error_text || "Run failed.");
  if (status === "stopped") return "Run stopped.";
  return `Run ${status}.`;
}

function renderFlagChip(value) {
  const enabled = Boolean(value);
  const icon = enabled ? "check" : "x";
  const text = enabled ? "Yes" : "No";
  const cls = enabled ? "is-true" : "is-false";
  return `<span class="flag-chip ${cls}"><i data-lucide="${icon}"></i>${escapeHtml(text)}</span>`;
}

function renderShayanProgram(program) {
  const episodes = Array.isArray(program?.episodes) ? program.episodes : [];
  const stats = program?.stats || {};
  const summary = `${Number(stats.downloaded || 0)}/${Number(stats.episodes || episodes.length)} downloaded • ${Number(stats.uploaded || 0)} uploaded`;
  const rows = episodes
    .map((episode) => {
      const entryKey = String(episode?.entry_key || "");
      const seasonText = episode?.season === null || episode?.season === undefined ? "-" : String(episode.season);
      const episodeText = episode?.episode === null || episode?.episode === undefined ? "-" : String(episode.episode);
      const titleText = String(episode?.title || "-");
      return `
        <tr>
          <td>${escapeHtml(seasonText)}</td>
          <td>${escapeHtml(episodeText)}</td>
          <td>${escapeHtml(titleText)}</td>
          <td>${renderFlagChip(Boolean(episode?.downloaded))}</td>
          <td>${renderFlagChip(Boolean(episode?.uploaded))}</td>
          <td>
            <div class="shayan-episode-actions">
              <button
                class="icon-btn shayan-redownload-btn"
                data-entry-key="${escapeHtml(entryKey)}"
                title="Re-download episode"
                aria-label="Re-download episode"
              >
                <i data-lucide="rotate-cw"></i>
              </button>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
  return `
    <details class="shayan-program">
      <summary>
        <div>
          <div class="shayan-program-title">${escapeHtml(String(program?.program || "Unknown program"))}</div>
          <div class="shayan-program-meta">${escapeHtml(String(program?.category || "unknown"))}</div>
        </div>
        <div class="shayan-program-meta">${escapeHtml(summary)}</div>
      </summary>
      <div class="shayan-program-body">
        <div class="shayan-episodes-table-wrap">
          <table class="shayan-episodes-table">
            <thead>
              <tr>
                <th>Season</th>
                <th>Episode</th>
                <th>Title</th>
                <th>Downloaded</th>
                <th>Uploaded</th>
                <th></th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>
    </details>
  `;
}

function renderShayanCatalog(catalog) {
  const cardNode = document.getElementById("shayan-catalog-card");
  const statsNode = document.getElementById("shayan-catalog-stats");
  const listNode = document.getElementById("shayan-program-list");
  const subtitleNode = document.getElementById("shayan-catalog-subtitle");
  if (!cardNode || !statsNode || !listNode || !subtitleNode) return;
  state.shayanCatalog = catalog && typeof catalog === "object" ? catalog : null;
  const payload = state.shayanCatalog || {};
  const stats = payload.stats && typeof payload.stats === "object" ? payload.stats : {};
  const programs = Array.isArray(payload.programs) ? payload.programs : [];

  cardNode.hidden = false;
  subtitleNode.textContent = `${Number(stats.programs || 0)} programs • ${Number(stats.episodes || 0)} episodes`;
  statsNode.innerHTML = [
    { label: "Programs", value: Number(stats.programs || 0) },
    { label: "Episodes", value: Number(stats.episodes || 0) },
    { label: "Downloaded", value: Number(stats.downloaded || 0) },
    { label: "Uploaded", value: Number(stats.uploaded || 0) },
  ]
    .map(
      (item) =>
        `<div class="stat"><div class="stat-label">${escapeHtml(String(item.label))}</div><div class="stat-value">${escapeHtml(String(item.value))}</div></div>`
    )
    .join("");
  listNode.innerHTML = programs.length
    ? programs.map(renderShayanProgram).join("")
    : '<div class="run-row">No known episodes in current source snapshot.</div>';
  lucide.createIcons();
}

function hideShayanCatalog() {
  const cardNode = document.getElementById("shayan-catalog-card");
  if (cardNode) {
    cardNode.hidden = true;
  }
  state.shayanCatalog = null;
}

async function refreshShayanCatalog() {
  const cardNode = document.getElementById("shayan-catalog-card");
  if (!cardNode) return;
  const payload = await api("/api/shayan/catalog");
  renderShayanCatalog(payload);
}

function markEpisodePendingRedownload(entryKey) {
  const catalog = state.shayanCatalog;
  if (!catalog || !Array.isArray(catalog.programs)) return;
  for (const program of catalog.programs) {
    const episodes = Array.isArray(program?.episodes) ? program.episodes : [];
    for (const episode of episodes) {
      if (String(episode?.entry_key || "") !== String(entryKey || "")) continue;
      episode.downloaded = false;
      episode.uploaded = false;
      return;
    }
  }
}

function renderTaskRuns(runs, taskTitle) {
  if (!Array.isArray(runs) || runs.length === 0) {
    return '<div class="run-row">No runs yet.</div>';
  }
  return runs
    .map((run) => {
      const runId = Number(run?.run_id || 0);
      const logsButton = runId
        ? `<button class="small-btn flow-run-logs" data-run-id="${runId}" data-task-title="${escapeHtml(taskTitle)}">Logs</button>`
        : "";
      return `
        <div class="flow-task-run-row task-status-${cssName(run?.status || "idle", "idle")}">
          <div class="flow-task-run-head">
            <span class="task-run-id">#${escapeHtml(String(runId || "-"))}</span>
            ${window.ManzaraCore.renderTaskStatusBadge(run || {}, { compact: true })}
            <span class="task-run-time">${escapeHtml(formatDateTime(run?.started_at))}</span>
          </div>
          <div class="flow-task-run-summary">${escapeHtml(runSummaryMessage(run))}</div>
          <div class="flow-task-run-actions">${logsButton}</div>
        </div>
      `;
    })
    .join("");
}

function renderTaskCard(task) {
  const model = taskControlModel(task);
  const runStatus = task.run?.status || "idle";
  const runId = task.run?.run_id || "";
  const logsDisabled = !runId;
  const summaryText = runSummaryMessage(task.run || {});
  const taskPathKey = encodeURIComponent(task.slug || task.task_id);
  const progress = task.run?.progress && typeof task.run.progress === "object"
    ? task.run.progress
    : {};
  const progressPercent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  const hasDeterminateProgress = model.showProgress && Number(progress.total || 0) > 0;
  const progressHtml = model.showProgress
    ? hasDeterminateProgress
      ? `
        <div class="progress-meta">
          <span>${escapeHtml(String(progress.current || 0))} / ${escapeHtml(String(progress.total || 0))}</span>
          <span>${escapeHtml(String(Math.round(progressPercent)))}%</span>
        </div>
        <div class="progress-wrap" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${escapeHtml(String(Math.round(progressPercent)))}">
          <div class="progress-determinate ${model.progressClass}" style="width: ${progressPercent}%"></div>
        </div>`
      : `<div class="progress-wrap"><div class="progress-indeterminate ${model.progressClass}"></div></div>`
    : "";

  return `
    <article class="task-card task-type-${cssName(task.task_type, "generic")} task-status-${cssName(runStatus, "idle")}">
      <div class="task-card-head">
        <div class="task-type-chip">${escapeHtml(task.task_type)}</div>
        <div class="task-title-row">
          <a class="task-title task-detail-link" href="/tasks/${taskPathKey}">${escapeHtml(task.title)}</a>
        </div>
        ${window.ManzaraCore.renderTaskStatusBadge(task.run || {}, { label: model.statusLabel })}
      </div>
      ${progressHtml}
      <div class="workflow-footnote">${escapeHtml(summaryText)}</div>
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
          title="Show latest logs"
          aria-label="Show latest logs"
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
      <div class="flow-task-runs">
        ${renderTaskRuns(task.runs || [], task.title)}
      </div>
    </article>
  `;
}

function renderGlobalState(payload) {
  const active = payload.global.active_tasks || 0;
  const activeWorkflows = payload.global.active_workflows || 0;
  document.getElementById("global-status").textContent = window.ManzaraCore.formatGlobalStatus(
    active,
    activeWorkflows
  );
  const stopBtn = document.getElementById("stop-all-btn");
  window.ManzaraCore.applyStopAllButton(stopBtn, payload.global.stop_all_state);
}

function ensureCanonicalFlowPath(flowPathKey) {
  const canonical = `/flows/${encodeURIComponent(flowPathKey)}`;
  if (window.location.pathname !== canonical) {
    window.history.replaceState({}, "", canonical);
  }
}

function renderFlow(payload) {
  viewState.set("ready");
  state.payload = payload;

  const flow = payload.flow || {};
  ensureCanonicalFlowPath(flow.slug || flow.panel_id || state.flowKey);
  document.getElementById("flow-title").textContent = String(flow.title || "Flow");
  document.getElementById("flow-subtitle").textContent = `${String(flow.panel_id || "")} • ${String(flow.description || "")}`.trim();

  const statsCards = Array.isArray(flow.stats_cards) ? flow.stats_cards : [];
  const statsHtml = statsCards.length
    ? statsCards
        .map((item) => {
          const label = String(item?.label || "");
          const rawValue = item?.value;
          const value =
            typeof rawValue === "string" && rawValue.includes("T") && !Number.isNaN(Date.parse(rawValue))
              ? formatDateTime(rawValue)
              : String(rawValue ?? "-");
          return `<div class="stat"><div class="stat-label">${escapeHtml(label)}</div><div class="stat-value">${escapeHtml(value)}</div></div>`;
        })
        .join("")
    : '<div class="run-row">No flow statistics available.</div>';
  document.getElementById("flow-stat-grid").innerHTML = statsHtml;

  const tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
  if (!tasks.length) {
    viewState.set("empty");
    document.getElementById("flow-task-grid").innerHTML = '<div class="run-row">No tasks in this flow.</div>';
  } else {
    document.getElementById("flow-task-grid").innerHTML = tasks.map(renderTaskCard).join("");
  }

  renderGlobalState(payload);
  lucide.createIcons();
}

function renderFlowLoading() {
  viewState.set("loading");
  document.getElementById("flow-title").textContent = "Loading flow...";
  document.getElementById("flow-subtitle").textContent = "";
  document.getElementById("flow-stat-grid").innerHTML = "";
  document.getElementById("flow-task-grid").innerHTML = '<div class="run-row">Loading tasks...</div>';
  hideShayanCatalog();
}

function renderFlowError(error) {
  viewState.set("error");
  const message = String(error?.message || error || "Failed to load flow.");
  const safe = escapeHtml(message);
  document.getElementById("flow-title").textContent = "Flow unavailable";
  document.getElementById("flow-subtitle").textContent = safe;
  document.getElementById("flow-stat-grid").innerHTML = "";
  document.getElementById("flow-task-grid").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
  hideShayanCatalog();
}

function readFlowKeyFromPath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts.length < 2) return null;
  return decodeURIComponent(parts.slice(1).join("/"));
}

async function refreshFlow({ showLoading = false } = {}) {
  if (showLoading) {
    renderFlowLoading();
  }
  try {
    const payload = await api(`/api/flows/${encodeURIComponent(state.flowKey)}?limit_per_task=20`);
    renderFlow(payload);
    const panelId = String(payload?.flow?.panel_id || "");
    if (panelId === "shayan") {
      try {
        await refreshShayanCatalog();
      } catch (error) {
        console.error(error);
      }
    } else {
      hideShayanCatalog();
    }
  } catch (error) {
    renderFlowError(error);
    throw error;
  }
}

function queueRefresh(delayMs = 250) {
  window.ManzaraCore.scheduleRefresh(state, refreshFlow, delayMs);
}

function findTaskById(taskId) {
  const tasks = state.payload?.tasks;
  if (!Array.isArray(tasks)) return null;
  return tasks.find((item) => String(item?.task_id || "") === String(taskId || "")) || null;
}

function applyOptimisticTaskAction(taskId) {
  const task = findTaskById(taskId);
  if (!task) return;

  const run = task.run && typeof task.run === "object" ? task.run : {};
  const status = String(run.status || "idle");
  if (status === "starting" || status === "running") {
    task.run = { ...run, status: "stopping_graceful" };
  } else if (status === "stopping_graceful") {
    task.run = { ...run, status: "stopping_force" };
  } else {
    const nowIso = new Date().toISOString();
    task.run = {
      ...run,
      status: "starting",
      started_at: run.started_at || nowIso,
      finished_at: null,
      exit_code: null,
      error_text: null,
    };
  }
}

function applyTaskActionResult(taskId, result) {
  const task = findTaskById(taskId);
  if (!task || !result || typeof result !== "object") return;

  const action = String(result.action || "");
  const run = result.run && typeof result.run === "object" ? result.run : null;
  if (run) {
    task.run = { ...(task.run || {}), ...run };
    const runs = Array.isArray(task.runs) ? task.runs : [];
    task.runs = runs;
    const runId = Number(run.run_id || 0);
    if (runId > 0) {
      const index = runs.findIndex((item) => Number(item?.run_id || 0) === runId);
      if (index >= 0) {
        runs[index] = { ...runs[index], ...run };
      } else {
        runs.unshift({ ...run });
      }
    }
    return;
  }

  if (action === "start") {
    const nowIso = new Date().toISOString();
    task.run = {
      ...(task.run || {}),
      status: "starting",
      started_at: task.run?.started_at || nowIso,
      finished_at: null,
      exit_code: null,
      error_text: null,
    };
  }
}

async function toggleTask(taskId) {
  applyOptimisticTaskAction(taskId);
  if (state.payload) {
    renderFlow(state.payload);
  }
  const result = await api(`/api/tasks/${encodeURIComponent(taskId)}/toggle`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  maybeShowTaskActionError(result);
  applyTaskActionResult(taskId, result);
  if (state.payload) {
    renderFlow(state.payload);
  }
  queueRefresh(0);
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

async function redownloadEpisode(entryKey) {
  if (!entryKey) return;
  markEpisodePendingRedownload(entryKey);
  if (state.shayanCatalog) {
    renderShayanCatalog(state.shayanCatalog);
  }
  await api(`/api/shayan/episodes/${encodeURIComponent(entryKey)}/redownload`, {
    method: "POST",
    body: JSON.stringify({}),
  });
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
    initialCursor: window.ManzaraCore.eventCursorFromSnapshot(state.payload),
    getCursor: () => Number(state.eventCursor || 0),
    setCursor: (nextCursor) => {
      state.eventCursor = Number(nextCursor || 0);
    },
    onEvent: (payload, event) => {
      document.getElementById("last-event").textContent = window.ManzaraCore.formatEventBanner(payload);
      maybePlayTaskNotification(payload, event.lastEventId || "");
      const relevant = String(payload?.panel_id || "") === String(state.payload?.flow?.panel_id || "");
      if (relevant && window.ManzaraCore.applyTaskEventState(state.payload, payload)) {
        renderFlow(state.payload);
      }
      if (relevant && window.ManzaraCore.eventNeedsReconciliation(payload)) {
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

  document.getElementById("flow-task-grid").addEventListener("click", (event) => {
    const target = event.target.closest("button");
    if (!target) return;

    if (target.classList.contains("task-toggle")) {
      const taskId = target.dataset.taskId;
      if (taskId) {
        toggleTask(taskId).catch((error) => {
          console.error(error);
          window.ManzaraUI.toast(error?.message || String(error), { tone: "error" });
        });
      }
      return;
    }

    if (target.classList.contains("task-logs") || target.classList.contains("flow-run-logs")) {
      const runId = Number(target.dataset.runId || "0");
      const taskTitle = target.dataset.taskTitle || "Task";
      if (runId > 0) {
        state.logViewer?.open(runId, taskTitle).catch((error) => console.error(error));
      }
    }
  });

  const programList = document.getElementById("shayan-program-list");
  if (programList) {
    programList.addEventListener("click", (event) => {
      const target = event.target.closest("button.shayan-redownload-btn");
      if (!target) return;
      const entryKey = String(target.dataset.entryKey || "");
      if (!entryKey) return;
      redownloadEpisode(entryKey).catch((error) => {
        console.error(error);
        window.ManzaraUI.toast(error?.message || String(error), { tone: "error" });
      });
    });
  }

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
  state.flowKey = readFlowKeyFromPath();
  if (!state.flowKey) {
    throw new Error("Flow id is missing in URL");
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
  await refreshFlow({ showLoading: true });
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  window.ManzaraUI.toast(error.message || String(error), { tone: "error" });
});
