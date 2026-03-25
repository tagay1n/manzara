const state = {
  payload: null,
  viewState: "loading",
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  soundNotifier: null,
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

const WEEKDAY_LABELS = {
  1: "Mon",
  2: "Tue",
  3: "Wed",
  4: "Thu",
  5: "Fri",
  6: "Sat",
  7: "Sun",
};

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
  if (!result || typeof result !== "object") return;
  const action = String(result.action || "");
  const reason = String(result?.reason || "");
  if (action === "noop" && result?.message) {
    window.alert(String(result.message));
    return;
  }
  if (reason.startsWith("sudo_") && result?.message) {
    window.alert(String(result.message));
  }
}

function workflowStatusModel(workflow) {
  const status = workflow.run?.status || "idle";
  if (status === "running" || status === "starting") {
    return {
      label: status === "starting" ? "Starting" : "Running",
      className: "state-running",
      runDisabled: true,
      icon: "loader-circle",
    };
  }
  if (status === "failed") {
    return {
      label: "Failed",
      className: "state-attention",
      runDisabled: false,
      icon: "circle-alert",
    };
  }
  if (status === "completed") {
    return {
      label: "Completed",
      className: "state-healthy",
      runDisabled: false,
      icon: "circle-check",
    };
  }
  if (status === "stopped") {
    return {
      label: "Stopped",
      className: "state-attention",
      runDisabled: false,
      icon: "square",
    };
  }
  return {
    label: "Idle",
    className: "",
    runDisabled: false,
    icon: "play",
  };
}

function renderWorkflowCard(workflow) {
  const model = workflowStatusModel(workflow);
  const schedule = workflow.schedule;

  if (!schedule) {
    return `
      <section class="workflow-card workflow-status-${cssName(workflow.run?.status || "idle")}">
        <div class="workflow-card-head">
          <div>
            <div class="workflow-title">${escapeHtml(workflow.title)}</div>
            <div class="workflow-description">${escapeHtml(workflow.description || "")}</div>
          </div>
          <div class="panel-pill ${model.className}">${escapeHtml(model.label)}</div>
        </div>
        <div class="workflow-footnote">No schedule configured.</div>
      </section>
    `;
  }

  const scheduleEnabled = Boolean(schedule.enabled);
  const scheduleId = schedule.schedule_id;
  const dayOfWeek = Number(schedule.day_of_week || 1);
  const timeOfDay = schedule.time_of_day || "03:00";
  const timezone = schedule.timezone || "UTC";
  const scheduleType = String(schedule.schedule_type || "weekly").toLowerCase();
  const isInterval = scheduleType === "interval";
  const intervalMinutes = Number(schedule.interval_minutes || 180);

  const scheduleToggleTitle = scheduleEnabled
    ? `Disable ${isInterval ? "interval" : "weekly"} schedule`
    : `Enable ${isInterval ? "interval" : "weekly"} schedule`;

  const weekdayOptions = Object.entries(WEEKDAY_LABELS)
    .map(([value, label]) => {
      const selected = Number(value) === dayOfWeek ? "selected" : "";
      return `<option value="${value}" ${selected}>${label}</option>`;
    })
    .join("");

  const scheduleControls = isInterval
    ? `
        <input
          type="number"
          class="schedule-interval"
          min="1"
          step="1"
          value="${escapeHtml(String(Math.max(1, intervalMinutes)))}"
          data-schedule-id="${escapeHtml(scheduleId)}"
          title="Interval minutes"
        />
      `
    : `
        <select class="schedule-day" data-schedule-id="${escapeHtml(scheduleId)}">
          ${weekdayOptions}
        </select>

        <input
          type="time"
          class="schedule-time"
          value="${escapeHtml(timeOfDay)}"
          data-schedule-id="${escapeHtml(scheduleId)}"
        />
      `;

  const scheduleFootnote = isInterval
    ? `Every ${Math.max(1, intervalMinutes)} min in ${escapeHtml(timezone)} | overlap: ${escapeHtml(schedule.overlap_policy || "skip")} | catch-up: ${escapeHtml(schedule.catchup_policy || "once")}`
    : `Weekly in ${escapeHtml(timezone)} | overlap: ${escapeHtml(schedule.overlap_policy || "skip")} | catch-up: ${escapeHtml(schedule.catchup_policy || "once")}`;

  return `
    <section class="workflow-card workflow-status-${cssName(workflow.run?.status || "idle")}">
      <div class="workflow-card-head">
        <div>
          <div class="workflow-title">${escapeHtml(workflow.title)}</div>
          <div class="workflow-description">${escapeHtml(workflow.description || "")}</div>
        </div>
        <div class="panel-pill ${model.className}">${escapeHtml(model.label)}</div>
      </div>

      <div class="workflow-meta-grid">
        <div><span class="meta-k">Last Run</span><span class="meta-v">${escapeHtml(formatDateTime(workflow.run?.finished_at || workflow.run?.started_at))}</span></div>
        <div><span class="meta-k">Next Run</span><span class="meta-v">${escapeHtml(formatDateTime(schedule.next_run_at))}</span></div>
      </div>

      <div class="workflow-controls" data-schedule-type="${escapeHtml(scheduleType)}">
        <button
          class="icon-btn workflow-run-now ${model.runDisabled ? "" : "active"}"
          title="Run workflow now"
          aria-label="Run workflow now"
          data-workflow-id="${escapeHtml(workflow.workflow_id)}"
          ${model.runDisabled ? "disabled" : ""}
        >
          <i data-lucide="${model.icon}"></i>
        </button>

        <button
          class="icon-btn schedule-toggle ${scheduleEnabled ? "active" : ""}"
          title="${escapeHtml(scheduleToggleTitle)}"
          aria-label="${escapeHtml(scheduleToggleTitle)}"
          data-schedule-id="${escapeHtml(scheduleId)}"
          data-schedule-enabled="${scheduleEnabled ? "1" : "0"}"
        >
          <i data-lucide="${scheduleEnabled ? "calendar-check" : "calendar"}"></i>
        </button>
        ${scheduleControls}

        <button
          class="icon-btn schedule-save"
          title="Save schedule"
          aria-label="Save schedule"
          data-schedule-id="${escapeHtml(scheduleId)}"
        >
          <i data-lucide="save"></i>
        </button>
      </div>

      <div class="workflow-footnote">${scheduleFootnote}</div>
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

function renderSchedules(payload) {
  viewState.set("ready");
  state.payload = payload;
  const scheduleGrid = document.getElementById("schedule-grid");
  const workflows = payload.workflows || [];
  if (!workflows.length) {
    viewState.set("empty");
    scheduleGrid.innerHTML = '<div class="run-row">No schedules available yet.</div>';
  } else {
    scheduleGrid.innerHTML = workflows.map(renderWorkflowCard).join("");
  }
  renderGlobalState(payload);
  lucide.createIcons();
}

function renderSchedulesLoading() {
  viewState.set("loading");
  document.getElementById("schedule-grid").innerHTML = '<div class="run-row">Loading schedules...</div>';
}

function renderSchedulesError(error) {
  viewState.set("error");
  const message = String(error?.message || error || "Failed to load schedules.");
  document.getElementById("schedule-grid").innerHTML = `<div class="run-row">Error: ${escapeHtml(message)}</div>`;
}

async function refreshSchedules({ showLoading = false } = {}) {
  if (showLoading) {
    renderSchedulesLoading();
  }
  try {
    const payload = await api("/api/schedules");
    renderSchedules(payload);
  } catch (error) {
    renderSchedulesError(error);
    throw error;
  }
}

function queueRefresh(delayMs = 250) {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(async () => {
    state.refreshTimer = null;
    try {
      await refreshSchedules();
    } catch (error) {
      console.error(error);
    }
  }, delayMs);
}

async function runWorkflowNow(workflowId) {
  const result = await api(`/api/workflows/${encodeURIComponent(workflowId)}/run`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  maybeShowTaskActionError(result);
  queueRefresh(0);
}

async function patchSchedule(scheduleId, patch) {
  await api(`/api/schedules/${encodeURIComponent(scheduleId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
  queueRefresh(0);
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

  document.getElementById("schedule-grid").addEventListener("click", (event) => {
    const target = event.target.closest("button");
    if (!target) return;

    if (target.classList.contains("workflow-run-now")) {
      const workflowId = target.dataset.workflowId;
      if (workflowId) {
        runWorkflowNow(workflowId).catch((error) => console.error(error));
      }
      return;
    }

    if (target.classList.contains("schedule-toggle")) {
      const scheduleId = target.dataset.scheduleId;
      if (scheduleId) {
        const enabled = target.dataset.scheduleEnabled === "1";
        patchSchedule(scheduleId, { enabled: !enabled }).catch((error) => console.error(error));
      }
      return;
    }

    if (target.classList.contains("schedule-save")) {
      const card = target.closest(".workflow-card");
      const scheduleId = target.dataset.scheduleId;
      if (!card || !scheduleId) return;
      const scheduleType = String(
        card.querySelector(".workflow-controls")?.dataset.scheduleType || "weekly",
      ).toLowerCase();

      if (scheduleType === "interval") {
        const interval = Number(card.querySelector(".schedule-interval")?.value || "0");
        if (!Number.isFinite(interval) || interval < 1) {
          window.alert("Interval must be an integer >= 1 minute");
          return;
        }
        patchSchedule(scheduleId, {
          schedule_type: "interval",
          interval_minutes: Math.floor(interval),
        }).catch((error) => console.error(error));
        return;
      }

      const day = Number(card.querySelector(".schedule-day")?.value || "1");
      const time = String(card.querySelector(".schedule-time")?.value || "03:00").trim();
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) {
        window.alert("Time must be HH:MM");
        return;
      }

      patchSchedule(scheduleId, {
        schedule_type: "weekly",
        day_of_week: day,
        time_of_day: time,
      }).catch((error) => console.error(error));
    }
  });

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
  await refreshSchedules({ showLoading: true });
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
});
