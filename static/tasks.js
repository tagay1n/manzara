const state = {
  payload: null,
  viewState: "loading",
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  soundNotifier: null,
};

async function api(path, options = {}) {
  return window.ManzaraCore.api(path, options);
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

function renderTaskItem(task) {
  const status = task.run?.status || "idle";
  const active = window.ManzaraCore.isActiveStatus(status);
  const taskPathKey = encodeURIComponent(task.slug || task.task_id);
  return `
    <a href="/tasks/${taskPathKey}" class="task-list-item task-status-${window.ManzaraCore.cssName(status, "idle")}">
      <div class="task-list-title">${window.ManzaraCore.escapeHtml(task.title)}</div>
      <div class="task-list-meta">
        <span>${window.ManzaraCore.escapeHtml(task.task_type)}</span>
        <span>${window.ManzaraCore.escapeHtml(active ? "active" : status)}</span>
      </div>
      <div class="task-list-time">${window.ManzaraCore.escapeHtml(formatDateTime(task.run?.started_at || task.run?.finished_at))}</div>
    </a>
  `;
}

function renderTaskFlow(flow) {
  return `
    <section class="task-flow-card">
      <div class="task-flow-head">
        <h3>${window.ManzaraCore.escapeHtml(flow.title)}</h3>
        <span class="panel-pill">Tasks ${flow.tasks.length}</span>
      </div>
      <div class="task-list-grid">
        ${(flow.tasks || []).map(renderTaskItem).join("")}
      </div>
    </section>
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

function renderTasks(payload) {
  state.payload = payload;
  const flowGrid = document.getElementById("task-flow-grid");
  const flows = payload.flows || [];
  const taskCount = flows.reduce((acc, flow) => acc + Number(flow.tasks?.length || 0), 0);
  if (taskCount === 0) {
    state.viewState = "empty";
    flowGrid.innerHTML = '<div class="run-row">No tasks available yet.</div>';
  } else {
    state.viewState = "ready";
    flowGrid.innerHTML = flows.map(renderTaskFlow).join("");
  }
  renderGlobalState(payload);
  lucide.createIcons();
}

function renderTasksLoading() {
  state.viewState = "loading";
  document.getElementById("task-flow-grid").innerHTML = '<div class="run-row">Loading tasks...</div>';
}

function renderTasksError(error) {
  state.viewState = "error";
  const message = String(error?.message || error || "Failed to load tasks.");
  document.getElementById("task-flow-grid").innerHTML = `<div class="run-row">Error: ${window.ManzaraCore.escapeHtml(message)}</div>`;
}

async function refreshTasks({ showLoading = false } = {}) {
  if (showLoading) {
    renderTasksLoading();
  }
  try {
    const payload = await api("/api/tasks");
    renderTasks(payload);
  } catch (error) {
    renderTasksError(error);
    throw error;
  }
}

function queueRefresh(delayMs = 250) {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(async () => {
    state.refreshTimer = null;
    try {
      await refreshTasks();
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
      queueRefresh(100);
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
  await refreshTasks({ showLoading: true });
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  alert(error.message || String(error));
});
