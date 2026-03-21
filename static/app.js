const state = {
  dashboard: null,
  refreshTimer: null,
  logRunId: null,
  logAfterId: 0,
  logPollTimer: null,
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

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function lucideName(name) {
  return String(name)
    .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
    .replaceAll("_", "-")
    .toLowerCase();
}

function cssName(name, fallback = "unknown") {
  const value = String(name || "").trim().toLowerCase();
  if (!value) return fallback;
  return value.replace(/[^a-z0-9_-]+/g, "-");
}

function isActiveStatus(status) {
  return (
    status === "starting" ||
    status === "running" ||
    status === "stopping_graceful" ||
    status === "stopping_force"
  );
}

function isWorkflowActive(status) {
  return status === "starting" || status === "running";
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
    icon: lucideName(task.icon_idle),
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

function renderWorkflowCard(workflow) {
  const model = workflowStatusModel(workflow);
  const schedule = workflow.schedule;

  const scheduleEnabled = Boolean(schedule?.enabled);
  const scheduleId = schedule?.schedule_id || "";
  const dayOfWeek = Number(schedule?.day_of_week || 1);
  const timeOfDay = schedule?.time_of_day || "03:00";
  const timezone = schedule?.timezone || "UTC";

  const scheduleToggleTitle = scheduleEnabled
    ? "Disable weekly schedule"
    : "Enable weekly schedule";

  const weekdayOptions = Object.entries(WEEKDAY_LABELS)
    .map(([value, label]) => {
      const selected = Number(value) === dayOfWeek ? "selected" : "";
      return `<option value="${value}" ${selected}>${label}</option>`;
    })
    .join("");

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
        <div><span class="meta-k">Next Run</span><span class="meta-v">${escapeHtml(formatDateTime(schedule?.next_run_at))}</span></div>
      </div>

      <div class="workflow-controls">
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
          ${schedule ? "" : "disabled"}
        >
          <i data-lucide="${scheduleEnabled ? "calendar-check" : "calendar"}"></i>
        </button>

        <select class="schedule-day" data-schedule-id="${escapeHtml(scheduleId)}" ${schedule ? "" : "disabled"}>
          ${weekdayOptions}
        </select>

        <input
          type="time"
          class="schedule-time"
          value="${escapeHtml(timeOfDay)}"
          data-schedule-id="${escapeHtml(scheduleId)}"
          ${schedule ? "" : "disabled"}
        />

        <button
          class="icon-btn schedule-save"
          title="Save schedule"
          aria-label="Save schedule"
          data-schedule-id="${escapeHtml(scheduleId)}"
          ${schedule ? "" : "disabled"}
        >
          <i data-lucide="save"></i>
        </button>
      </div>

      <div class="workflow-footnote">Weekly in ${escapeHtml(timezone)} | overlap: skip | catch-up: once</div>
    </section>
  `;
}

function renderPanel(panel) {
  const health = panelHealth(panel);
  const scanActive = panel.tasks.some(
    (task) => task.task_type === "scan" && isActiveStatus(task.run?.status || "idle")
  );
  const downloadActive = panel.tasks.some(
    (task) =>
      task.task_type === "download" && isActiveStatus(task.run?.status || "idle")
  );

  const workflowHtml = `
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

  const tasksHtml = panel.tasks
    .map((task) => {
      const model = taskControlModel(task);
      const runStatus = task.run?.status || "idle";
      const runId = task.run?.run_id || "";
      const logsDisabled = !runId;
      const statusText = model.label;
      const taskClass = `task-card task-type-${cssName(task.task_type, "generic")} task-status-${cssName(runStatus, "idle")}`;
      const progressHtml = model.showProgress
        ? `<div class="progress-wrap"><div class="progress-indeterminate ${model.progressClass}"></div></div>`
        : "";

      return `
        <div class="${taskClass}">
          <div class="task-card-head">
            <div class="task-type-chip">${escapeHtml(task.task_type)}</div>
            <div class="task-title">${escapeHtml(task.title)}</div>
            <div class="task-status">${escapeHtml(statusText)}</div>
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
          </div>
        </div>
      `;
    })
    .join("");

  const workflowsHtml = (panel.workflows || []).map(renderWorkflowCard).join("");
  const stats = panel.stats;

  return `
    <section class="panel">
      <div class="panel-head">
        <div class="panel-head-left">
          <div class="panel-kicker">Service</div>
          <h2>${escapeHtml(panel.title)}</h2>
        </div>
        <div class="panel-head-right">
          <span class="panel-pill ${health.className}">${health.label}</span>
          <span class="panel-pill">Active ${health.active}</span>
          <span class="panel-pill">Failed ${health.failed}</span>
        </div>
      </div>
      ${workflowHtml}
      <div class="stats">
        <div class="stat"><div class="stat-label">Downloaded Files</div><div class="stat-value">${stats.downloaded_files_total}</div></div>
        <div class="stat"><div class="stat-label">New Last Run</div><div class="stat-value">${stats.newly_downloaded_last_run}</div></div>
        <div class="stat"><div class="stat-label">Failed Last Run</div><div class="stat-value">${stats.failed_last_run}</div></div>
        <div class="stat"><div class="stat-label">Last Success</div><div class="stat-value">${escapeHtml(formatDateTime(stats.last_successful_run))}</div></div>
      </div>
      <div class="tasks">${tasksHtml}</div>
      <div class="workflow-stack">${workflowsHtml}</div>
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
        <div>${escapeHtml(run.task_id)} • ${escapeHtml(run.status)}</div>
        <div>${escapeHtml(formatDateTime(run.started_at))}</div>
      </div>
    `
    )
    .join("");
}

function renderDashboard(payload) {
  state.dashboard = payload;

  document.getElementById("panel-grid").innerHTML = payload.panels.map(renderPanel).join("");
  document.getElementById("runs-list").innerHTML = renderRuns(payload.recent_runs || []);

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

  lucide.createIcons();
}

async function refreshDashboard() {
  const payload = await api("/api/dashboard");
  renderDashboard(payload);
}

function queueRefresh(delayMs = 250) {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(async () => {
    state.refreshTimer = null;
    try {
      await refreshDashboard();
    } catch (error) {
      console.error(error);
    }
  }, delayMs);
}

async function toggleTask(taskId) {
  await api(`/api/tasks/${encodeURIComponent(taskId)}/toggle`, { method: "POST" });
  queueRefresh(0);
}

async function runWorkflowNow(workflowId) {
  await api(`/api/workflows/${encodeURIComponent(workflowId)}/run`, { method: "POST" });
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
  const stopState = state.dashboard?.global?.stop_all_state;
  if (stopState === "armed") {
    const confirmed = window.confirm("Force stop all running tasks immediately?");
    if (!confirmed) return;
  }
  await api("/api/system/stop-all", { method: "POST" });
  queueRefresh(0);
}

async function fetchLogs() {
  if (!state.logRunId) return;
  const response = await api(
    `/api/runs/${state.logRunId}/logs?after_log_id=${state.logAfterId}&limit=400`
  );
  const content = document.getElementById("log-content");
  for (const line of response.lines) {
    content.textContent += `${line.line}\n`;
  }
  state.logAfterId = response.next_after_log_id;
  content.scrollTop = content.scrollHeight;
}

async function openLogs(runId, taskTitle) {
  state.logRunId = runId;
  state.logAfterId = 0;
  document.getElementById("log-title").textContent = `Logs • ${taskTitle} • run ${runId}`;
  document.getElementById("log-content").textContent = "";
  const dialog = document.getElementById("log-dialog");
  if (!dialog.open) {
    dialog.showModal();
  }

  await fetchLogs();

  if (state.logPollTimer) {
    clearInterval(state.logPollTimer);
  }
  state.logPollTimer = setInterval(() => {
    fetchLogs().catch((error) => console.error(error));
  }, 1500);
}

function closeLogs() {
  const dialog = document.getElementById("log-dialog");
  if (dialog.open) {
    dialog.close();
  }
  if (state.logPollTimer) {
    clearInterval(state.logPollTimer);
    state.logPollTimer = null;
  }
  state.logRunId = null;
  state.logAfterId = 0;
}

function setupEventStream() {
  const stream = new EventSource("/api/events/stream");
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
        document.getElementById("last-event").textContent = `Last event: ${payload.type} @ ${new Date(payload.ts).toLocaleTimeString()}`;
      } catch (_error) {
        // ignore malformed events
      }
      queueRefresh(100);
    });
  });

  stream.onerror = () => {
    setTimeout(setupEventStream, 1500);
  };
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

    if (target.classList.contains("task-logs")) {
      const runId = Number(target.dataset.runId || "0");
      const taskTitle = target.dataset.taskTitle || "Task";
      if (runId > 0) {
        openLogs(runId, taskTitle).catch((error) => console.error(error));
      }
      return;
    }

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

      const day = Number(card.querySelector(".schedule-day")?.value || "1");
      const time = String(card.querySelector(".schedule-time")?.value || "03:00").trim();
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) {
        window.alert("Time must be HH:MM");
        return;
      }

      patchSchedule(scheduleId, { day_of_week: day, time_of_day: time }).catch((error) => console.error(error));
    }
  });

  document.getElementById("stop-all-btn").addEventListener("click", () => {
    stopAll().catch((error) => console.error(error));
  });

  document.getElementById("close-logs").addEventListener("click", closeLogs);
  document.getElementById("log-dialog").addEventListener("close", closeLogs);

  document.getElementById("copy-logs").addEventListener("click", async () => {
    const text = document.getElementById("log-content").textContent;
    await navigator.clipboard.writeText(text || "");
  });
}

async function bootstrap() {
  attachUiHandlers();
  await refreshDashboard();
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
});
