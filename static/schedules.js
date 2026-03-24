const state = {
  payload: null,
  refreshTimer: null,
  eventStream: null,
  eventStreamReconnectTimer: null,
  eventCursor: 0,
  soundNotifier: null,
  sudoPrompt: null,
};

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

function cssName(name, fallback = "unknown") {
  const value = String(name || "").trim().toLowerCase();
  if (!value) return fallback;
  return value.replace(/[^a-z0-9_-]+/g, "-");
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

function initSudoPrompt() {
  const createPrompt = window.ManzaraSudoPrompt?.createPrompt;
  if (typeof createPrompt !== "function") return;
  state.sudoPrompt = createPrompt();
}

function teardownSudoPrompt() {
  if (state.sudoPrompt && typeof state.sudoPrompt.teardown === "function") {
    state.sudoPrompt.teardown();
  }
  state.sudoPrompt = null;
}

async function runWithSudoPrompt(requestExecutor, contextLabel) {
  const runWithPrompt = window.ManzaraSudoPrompt?.runWithSudoPrompt;
  if (typeof runWithPrompt !== "function") {
    return requestExecutor(null);
  }
  return runWithPrompt({
    execute: requestExecutor,
    prompt: state.sudoPrompt,
    contextLabel,
  });
}

function maybeShowSudoError(result) {
  const reason = String(result?.reason || "");
  if (reason === "sudo_prompt_cancelled") return;
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

function renderSchedules(payload) {
  state.payload = payload;
  document.getElementById("schedule-grid").innerHTML = (payload.workflows || [])
    .map(renderWorkflowCard)
    .join("");
  renderGlobalState(payload);
  lucide.createIcons();
}

async function refreshSchedules() {
  const payload = await api("/api/schedules");
  renderSchedules(payload);
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
  const result = await runWithSudoPrompt(
    (sudoPassword) =>
      api(`/api/workflows/${encodeURIComponent(workflowId)}/run`, {
        method: "POST",
        body: JSON.stringify(sudoPassword ? { sudo_password: sudoPassword } : {}),
      }),
    "Workflow requires sudo access"
  );
  maybeShowSudoError(result);
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
  initSudoPrompt();
  window.addEventListener("beforeunload", () => {
    teardownSoundNotifier();
    teardownSudoPrompt();
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
  await refreshSchedules();
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
});
