const state = {
  payload: null,
  viewState: "loading",
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  soundNotifier: null,
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

async function api(path, options = {}) {
  return window.ManzaraCore.api(path, options);
}

function escapeHtml(value) {
  return window.ManzaraCore.escapeHtml(value);
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
  return window.ManzaraCore.cssName(name, fallback);
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
  document.getElementById("global-status").textContent = window.ManzaraCore.formatGlobalStatus(
    active,
    activeWorkflows
  );
  const stopBtn = document.getElementById("stop-all-btn");
  window.ManzaraCore.applyStopAllButton(stopBtn, payload.global.stop_all_state);
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
  viewState.set("ready");
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

function renderDatabaseLoading() {
  viewState.set("loading");
  document.getElementById("db-warning-pill").className = "panel-pill";
  document.getElementById("db-warning-pill").textContent = "Loading";
  document.getElementById("db-status").textContent = "Loading database state...";
  document.getElementById("db-stat-grid").innerHTML = '<div class="run-row">Loading database metrics...</div>';
  document.getElementById("db-backup-grid").innerHTML = '<div class="run-row">Loading backup state...</div>';
  document.getElementById("db-table-body").innerHTML = '<tr><td colspan="3">Loading table metrics...</td></tr>';
  document.getElementById("db-table-footnote").textContent = "";
}

function renderDatabaseError(error) {
  viewState.set("error");
  const message = String(error?.message || error || "Failed to load database state.");
  const safe = escapeHtml(message);
  document.getElementById("db-warning-pill").className = "panel-pill state-attention";
  document.getElementById("db-warning-pill").textContent = "Unavailable";
  document.getElementById("db-status").textContent = `Database state unavailable: ${message}`;
  document.getElementById("db-stat-grid").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
  document.getElementById("db-backup-grid").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
  document.getElementById("db-table-body").innerHTML = `<tr><td colspan="3">Error: ${safe}</td></tr>`;
  document.getElementById("db-table-footnote").textContent = "";
}

async function refreshDatabaseState({ showLoading = false } = {}) {
  if (showLoading) {
    renderDatabaseLoading();
  }
  try {
    const payload = await api("/api/database/state");
    renderDatabaseState(payload);
  } catch (error) {
    renderDatabaseError(error);
    throw error;
  }
}

function queueRefresh(delayMs = 250) {
  window.ManzaraCore.scheduleRefresh(state, refreshDatabaseState, delayMs);
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
      const eventType = String(payload?.type || "");
      const taskFinished = ["task.artifact", "task.completed", "task.failed", "task.stopped"]
        .includes(eventType);
      if (
        (["maintenance", "backup"].includes(String(payload?.panel_id || "")) && taskFinished)
        || eventType.startsWith("schedule.")
      ) {
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
  await refreshDatabaseState({ showLoading: true });
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  window.ManzaraUI.toast(error.message || String(error), { tone: "error" });
});
