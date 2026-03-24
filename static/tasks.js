const state = {
  payload: null,
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

function cssName(name, fallback = "unknown") {
  const value = String(name || "").trim().toLowerCase();
  if (!value) return fallback;
  return value.replace(/[^a-z0-9_-]+/g, "-");
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

function isActiveStatus(status) {
  return (
    status === "starting" ||
    status === "running" ||
    status === "stopping_graceful" ||
    status === "stopping_force"
  );
}

function renderTaskItem(task) {
  const status = task.run?.status || "idle";
  const active = isActiveStatus(status);
  const taskPathKey = encodeURIComponent(task.slug || task.task_id);
  return `
    <a href="/tasks/${taskPathKey}" class="task-list-item task-status-${cssName(status, "idle")}">
      <div class="task-list-title">${escapeHtml(task.title)}</div>
      <div class="task-list-meta">
        <span>${escapeHtml(task.task_type)}</span>
        <span>${escapeHtml(active ? "active" : status)}</span>
      </div>
      <div class="task-list-time">${escapeHtml(formatDateTime(task.run?.started_at || task.run?.finished_at))}</div>
    </a>
  `;
}

function renderTaskFlow(flow) {
  return `
    <section class="task-flow-card">
      <div class="task-flow-head">
        <h3>${escapeHtml(flow.title)}</h3>
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

function renderTasks(payload) {
  state.payload = payload;
  document.getElementById("task-flow-grid").innerHTML = (payload.flows || [])
    .map(renderTaskFlow)
    .join("");
  renderGlobalState(payload);
  lucide.createIcons();
}

async function refreshTasks() {
  const payload = await api("/api/tasks");
  renderTasks(payload);
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
      document.getElementById("last-event").textContent =
        "Last event: " + payload.type + " @ " + window.ManzaraCore.formatTime(payload.ts, { includeZone: true });
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
  await refreshTasks();
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  alert(error.message || String(error));
});
