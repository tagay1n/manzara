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

function formatDateTime(value) {
  return window.ManzaraCore.formatDateTime(value);
}

function formatInt(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return "0";
  return Math.floor(number).toLocaleString();
}

function formatBytes(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let current = number;
  let index = 0;
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024;
    index += 1;
  }
  const precision = current >= 100 || index === 0 ? 0 : current >= 10 ? 1 : 2;
  return `${current.toFixed(precision)} ${units[index]}`;
}

function cssName(name, fallback = "unknown") {
  const value = String(name || "").trim().toLowerCase();
  if (!value) return fallback;
  return value.replace(/[^a-z0-9_-]+/g, "-");
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

function renderBackupItem(title, item) {
  const run = item?.run || {};
  const schedule = item?.schedule || {};
  return `
    <div class="db-backup-row">
      <div class="db-backup-title">${escapeHtml(title)}</div>
      <div class="db-backup-meta">Run: ${escapeHtml(run.status || "idle")} (${escapeHtml(formatDateTime(run.finished_at || run.started_at))})</div>
      <div class="db-backup-meta">Schedule: ${schedule.enabled ? "enabled" : "disabled"} • Next: ${escapeHtml(formatDateTime(schedule.next_run_at))}</div>
    </div>
  `;
}

function renderDatabaseState(payload) {
  state.payload = payload;
  const db = payload.database_state || {};
  renderGlobalState(payload);

  const warningNode = document.getElementById("db-warning-pill");
  const statusNode = document.getElementById("db-status");
  const statsNode = document.getElementById("db-stat-grid");
  const backupsNode = document.getElementById("db-backup-grid");
  const tableBodyNode = document.getElementById("db-table-body");
  const footnoteNode = document.getElementById("db-table-footnote");

  if (!db.available) {
    warningNode.className = "panel-pill state-attention";
    warningNode.textContent = "Unavailable";
    statusNode.textContent = db.error || "Database snapshot unavailable";
    statsNode.innerHTML = "";
    backupsNode.innerHTML = `
      ${renderBackupItem("Full backup", db.backup?.full)}
      ${renderBackupItem("Incremental backup", db.backup?.incremental)}
    `;
    tableBodyNode.innerHTML = '<tr><td colspan="3">No table data.</td></tr>';
    footnoteNode.textContent = `Snapshot: ${formatDateTime(db.captured_at)}`;
    lucide.createIcons();
    return;
  }

  const warningLevel = cssName(db.disk?.warning_level || "ok");
  warningNode.className = `panel-pill db-warning-${warningLevel}`;
  warningNode.textContent =
    db.disk?.warning_text || "Disk metrics unavailable (insufficient DB privileges).";
  statusNode.textContent = `Snapshot: ${formatDateTime(db.captured_at)}`;
  const diskFree = db.disk ? formatBytes(db.disk.free_bytes || 0) : "-";
  const diskFreePct = db.disk ? `${String(db.disk.free_pct || 0)}%` : "-";

  statsNode.innerHTML = `
    <div class="stat"><div class="stat-label">Database</div><div class="stat-value">${escapeHtml(db.database_name || "-")}</div></div>
    <div class="stat"><div class="stat-label">Schema</div><div class="stat-value">${escapeHtml(db.schema || "-")}</div></div>
    <div class="stat"><div class="stat-label">DB Size</div><div class="stat-value">${escapeHtml(formatBytes(db.database_size_bytes || 0))}</div></div>
    <div class="stat"><div class="stat-label">Disk Free</div><div class="stat-value">${escapeHtml(diskFree)}</div></div>
    <div class="stat"><div class="stat-label">Disk Free %</div><div class="stat-value">${escapeHtml(diskFreePct)}</div></div>
    <div class="stat"><div class="stat-label">Data Directory</div><div class="stat-value">${escapeHtml(db.data_directory || "-")}</div></div>
  `;

  backupsNode.innerHTML = `
    ${renderBackupItem("Full backup", db.backup?.full)}
    ${renderBackupItem("Incremental backup", db.backup?.incremental)}
  `;

  const rows = (db.tables || [])
    .slice(0, 250)
    .map(
      (item) => `
      <tr>
        <td>${escapeHtml(item.table_name || "-")}</td>
        <td class="db-cell-num">${escapeHtml(formatInt(item.estimated_rows || 0))}</td>
        <td class="db-cell-num">${escapeHtml(formatBytes(item.total_bytes || 0))}</td>
      </tr>
    `
    )
    .join("");
  tableBodyNode.innerHTML = rows || '<tr><td colspan="3">No tables found.</td></tr>';
  footnoteNode.textContent = `Showing ${(db.tables || []).length} table(s) from schema ${db.schema || "-"}`;
  lucide.createIcons();
}

async function refreshDatabaseState() {
  const payload = await api("/api/database/state");
  renderDatabaseState(payload);
}

function queueRefresh(delayMs = 250) {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(async () => {
    state.refreshTimer = null;
    try {
      await refreshDatabaseState();
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
  await refreshDatabaseState();
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  alert(error.message || String(error));
});
