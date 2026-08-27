const state = {
  payload: null,
  viewState: "loading",
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  soundNotifier: null,
  conveyorController: null,
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

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

function taskToggleModel(status) {
  if (status === "stopping_force") {
    return { icon: "octagon-x", title: "Force stopping", disabled: true, cls: "red" };
  }
  if (status === "stopping_graceful") {
    return { icon: "octagon-x", title: "Force stop now", disabled: false, cls: "red" };
  }
  if (status === "starting" || status === "running") {
    return { icon: "square", title: "Request graceful stop", disabled: false, cls: "active" };
  }
  return { icon: "play", title: "Start task", disabled: false, cls: "" };
}

function renderTaskItem(task) {
  const status = task.run?.status || "idle";
  const control = taskToggleModel(status);
  const taskPathKey = encodeURIComponent(task.slug || task.task_id);
  return `
    <article class="task-list-item task-status-${window.ManzaraCore.cssName(status, "idle")}"
      draggable="true" data-conveyor-task-id="${window.ManzaraCore.escapeHtml(task.task_id)}">
      <a href="/tasks/${taskPathKey}" class="task-list-link">
        <div class="task-list-title">${window.ManzaraCore.escapeHtml(task.title)}</div>
        <div class="task-list-meta">
          <span>${window.ManzaraCore.escapeHtml(task.task_type)}</span>
          ${window.ManzaraCore.renderTaskStatusBadge(task.run || { status }, { compact: true })}
        </div>
        <div class="task-list-time">${window.ManzaraCore.escapeHtml(formatDateTime(task.run?.started_at || task.run?.finished_at))}</div>
      </a>
      <button class="icon-btn task-list-toggle ${control.cls}"
        type="button" data-task-toggle-id="${window.ManzaraCore.escapeHtml(task.task_id)}"
        title="${window.ManzaraCore.escapeHtml(control.title)}"
        aria-label="${window.ManzaraCore.escapeHtml(control.title)}"
        ${control.disabled ? "disabled" : ""}>
        <i data-lucide="${control.icon}"></i>
      </button>
    </article>
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
  document.getElementById("global-status").textContent = window.ManzaraCore.formatGlobalStatus(
    active
  );
  const stopBtn = document.getElementById("stop-all-btn");
  window.ManzaraCore.applyStopAllButton(stopBtn, payload.global.stop_all_state);
}

function renderTasks(payload) {
  state.payload = payload;
  state.conveyorController?.sync(payload);
  const flowGrid = document.getElementById("task-flow-grid");
  const flows = payload.flows || [];
  const taskCount = flows.reduce((acc, flow) => acc + Number(flow.tasks?.length || 0), 0);
  if (taskCount === 0) {
    viewState.set("empty");
    flowGrid.innerHTML = '<div class="run-row">No tasks available yet.</div>';
  } else {
    viewState.set("ready");
    flowGrid.innerHTML = flows.map(renderTaskFlow).join("");
  }
  renderGlobalState(payload);
  state.conveyorController?.render();
  lucide.createIcons();
}

function renderTasksLoading() {
  viewState.set("loading");
  state.conveyorController?.setLoading();
  document.getElementById("task-flow-grid").innerHTML = '<div class="run-row">Loading tasks...</div>';
}

function renderTasksError(error) {
  viewState.set("error");
  const message = String(error?.message || error || "Failed to load tasks.");
  document.getElementById("task-flow-grid").innerHTML = `<div class="run-row">Error: ${window.ManzaraCore.escapeHtml(message)}</div>`;
  state.conveyorController?.setError(error);
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
  window.ManzaraCore.scheduleRefresh(state, refreshTasks, delayMs);
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

async function toggleTask(taskId, button) {
  if (!taskId || button?.disabled) return;
  if (button) button.disabled = true;
  try {
    const result = await api(`/api/tasks/${encodeURIComponent(taskId)}/toggle`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    window.ManzaraUI?.reportTaskActionResult?.(result);
  } finally {
    if (button) button.disabled = false;
    queueRefresh(0);
  }
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
      state.conveyorController?.handleEvent(payload);
      if (window.ManzaraCore.applyTaskEventState(state.payload, payload)) {
        renderTasks(state.payload);
      }
      if (window.ManzaraCore.eventNeedsReconciliation(payload)) {
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
  document.getElementById("task-flow-grid").addEventListener("click", (event) => {
    const directTarget = event.target?.dataset?.taskToggleId ? event.target : null;
    const button = directTarget || event.target?.closest?.("[data-task-toggle-id]");
    if (!button) return;
    event.preventDefault?.();
    event.stopPropagation?.();
    toggleTask(String(button.dataset.taskToggleId || ""), button).catch((error) => {
      window.ManzaraUI.toast(error?.message || String(error), { tone: "error" });
    });
  });
}

async function bootstrap() {
  initSoundNotifier();
  state.conveyorController = window.ManzaraConveyor.createController({
    api,
    getPayload: () => state.payload,
    refresh: () => refreshTasks(),
  });
  window.addEventListener("beforeunload", () => {
    teardownSoundNotifier();
    if (state.eventStreamController) {
      state.eventStreamController.stop();
      state.eventStreamController = null;
    }

  });
  attachUiHandlers();
  state.conveyorController.attach();
  await refreshTasks({ showLoading: true });
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  window.ManzaraUI.toast(error.message || String(error), { tone: "error" });
});
