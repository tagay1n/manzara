const state = {
  tablePayload: null,
  insightsPayload: null,
  globalPayload: null,
  refreshTimer: null,
  eventStream: null,
  eventStreamReconnectTimer: null,
  eventCursor: 0,
  soundNotifier: null,
  page: 1,
  pageSize: 25,
  activeTab: "table",
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

function currentFilters() {
  return {
    search: document.getElementById("filter-search").value.trim(),
    ddcPrefix: document.getElementById("filter-ddc-prefix").value.trim(),
    minUsage: Number(document.getElementById("filter-min-usage").value || "0"),
    status: document.getElementById("filter-status").value,
    sort: document.getElementById("filter-sort").value,
  };
}

function tableUrl() {
  const filters = currentFilters();
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
    search: filters.search,
    ddc_prefix: filters.ddcPrefix,
    min_usage: String(Math.max(0, filters.minUsage || 0)),
    status: filters.status,
    sort: filters.sort,
  });
  return `/api/library/classifications?${params.toString()}`;
}

function renderTable(payload) {
  state.tablePayload = payload;
  const statusNode = document.getElementById("classification-table-status");
  if (!payload.available) {
    statusNode.textContent = `Table unavailable: ${payload.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
    document.getElementById("classification-table-body").innerHTML = "";
    return;
  }
  statusNode.classList.remove("library-status-error");
  statusNode.textContent = `Loaded ${payload.items.length} rows from ${payload.total} total`;

  document.getElementById("classification-table-body").innerHTML = (payload.items || [])
    .map(
      (item) => `
      <tr>
        <td>${item.classification_id}</td>
        <td><a href="/library/classifications/${item.classification_id}" class="run-task-link">${escapeHtml(item.ddc || "-")}</a></td>
        <td title="${escapeHtml(item.path || "-")}">${escapeHtml(item.path || "-")}</td>
        <td>${item.usage_count}</td>
        <td>${escapeHtml(item.status || "-")}</td>
        <td>${escapeHtml(item.created_by || "-")}</td>
        <td>${escapeHtml(formatDateTime(item.created_at))}</td>
      </tr>
    `
    )
    .join("");

  document.getElementById("page-label").textContent = `Page ${payload.page} / ${payload.total_pages}`;
  document.getElementById("page-prev").disabled = payload.page <= 1;
  document.getElementById("page-next").disabled = payload.page >= payload.total_pages;
}

function applyActiveTab() {
  const tabs = ["table", "tree", "distribution", "duplicates", "unclassified"];
  for (const tab of tabs) {
    const isActive = state.activeTab === tab;
    const btn = document.getElementById(`tab-btn-${tab}`);
    const panel = document.getElementById(`tab-panel-${tab}`);
    if (btn) {
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
    }
    if (panel) {
      panel.classList.toggle("active", isActive);
    }
  }
}

function switchTab(tab) {
  if (!["table", "tree", "distribution", "duplicates", "unclassified"].includes(tab)) return;
  state.activeTab = tab;
  applyActiveTab();
}

function renderTreeNodes(nodes, depth = 0) {
  if (!nodes || !nodes.length) {
    return depth === 0 ? '<div class="workflow-footnote">No hierarchy data.</div>' : "";
  }
  return `
    <ul class="tree-list ${depth === 0 ? "root" : ""}">
      ${nodes
        .map(
          (node) => `
        <li>
          <div class="tree-row">
            <span class="tree-name">${escapeHtml(node.name || "-")}</span>
            <span class="tree-count">${escapeHtml(String(node.usage_count || 0))}</span>
          </div>
          ${renderTreeNodes(node.children || [], depth + 1)}
        </li>
      `
        )
        .join("")}
    </ul>
  `;
}

function renderDistribution(items) {
  if (!items || !items.length) {
    return '<div class="workflow-footnote">No distribution data.</div>';
  }
  return items
    .map(
      (item) => `
      <div class="distribution-row">
        <div class="distribution-head">
          <span>${escapeHtml(item.bucket || "-")}</span>
          <span>${escapeHtml(String(item.usage_count || 0))} • ${escapeHtml(String(item.share_pct || 0))}%</span>
        </div>
        <div class="distribution-bar">
          <span style="width: ${Math.max(0, Math.min(100, Number(item.share_pct || 0)))}%"></span>
        </div>
      </div>
    `
    )
    .join("");
}

function renderDuplicates(items) {
  if (!items || !items.length) {
    return '<div class="workflow-footnote">No duplicates detected.</div>';
  }
  return items
    .map(
      (group) => `
      <div class="duplicate-card">
        <div class="duplicate-head">
          <span class="duplicate-path">${escapeHtml(group.path || "-")}</span>
          <span class="panel-pill">${escapeHtml(group.issue || "-")}</span>
        </div>
        <div class="workflow-footnote">Usage ${group.total_usage} • DDC variants ${group.distinct_ddc_count}</div>
        <div class="duplicate-items">
          ${(group.items || [])
            .map(
              (item) => `
            <a class="duplicate-item" href="/library/classifications/${item.classification_id}">
              <span>${escapeHtml(item.ddc || "-")}</span>
              <span>${escapeHtml(String(item.usage_count || 0))}</span>
            </a>
          `
            )
            .join("")}
        </div>
      </div>
    `
    )
    .join("");
}

function renderUnclassified(queue) {
  if (!queue || !queue.items || !queue.items.length) {
    return `<div class="workflow-footnote">Queue is empty.</div>`;
  }
  const rows = queue.items
    .map(
      (item) => `
      <div class="run-row">
        <div>${escapeHtml(item.md5 || "-")} • ${escapeHtml(item.language || "-")}</div>
        <div title="${escapeHtml(item.ya_path || "-")}">${escapeHtml(item.mime_type || "-")}</div>
      </div>
    `
    )
    .join("");
  return `
    <div class="workflow-footnote">Total in queue: ${queue.total || 0}</div>
    ${rows}
  `;
}

function renderInsights(payload) {
  state.insightsPayload = payload;
  if (!payload.available) {
    document.getElementById("tree-root").innerHTML = `<div class="workflow-footnote library-status-error">${escapeHtml(payload.error || "unknown error")}</div>`;
    document.getElementById("distribution-root").innerHTML = "";
    document.getElementById("duplicates-root").innerHTML = "";
    document.getElementById("unclassified-root").innerHTML = "";
    const duplicatesBadge = document.getElementById("tab-badge-duplicates");
    if (duplicatesBadge) duplicatesBadge.textContent = "0";
    const unclassifiedBadge = document.getElementById("tab-badge-unclassified");
    if (unclassifiedBadge) unclassifiedBadge.textContent = "0";
    return;
  }
  document.getElementById("tree-root").innerHTML = renderTreeNodes(payload.tree || []);
  document.getElementById("distribution-root").innerHTML = renderDistribution(payload.distribution || []);
  document.getElementById("duplicates-root").innerHTML = renderDuplicates(payload.duplicates || []);
  document.getElementById("unclassified-root").innerHTML = renderUnclassified(payload.unclassified_queue || {});
  const duplicatesBadge = document.getElementById("tab-badge-duplicates");
  if (duplicatesBadge) {
    duplicatesBadge.textContent = String((payload.duplicates || []).length);
  }
  const unclassifiedBadge = document.getElementById("tab-badge-unclassified");
  if (unclassifiedBadge) {
    unclassifiedBadge.textContent = String(
      Number(payload.unclassified_queue?.total || 0)
    );
  }
}

async function refreshTable() {
  const payload = await api(tableUrl());
  renderTable(payload);
}

async function refreshInsights() {
  const payload = await api("/api/library/classifications/insights");
  renderInsights(payload);
}

async function refreshGlobal() {
  const payload = await api("/api/library");
  state.globalPayload = payload;
  renderGlobalState(payload);
}

async function refreshAll() {
  await Promise.all([refreshTable(), refreshInsights(), refreshGlobal()]);
  applyActiveTab();
  lucide.createIcons();
}

function queueRefresh(delayMs = 250) {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(async () => {
    state.refreshTimer = null;
    try {
      await refreshAll();
    } catch (error) {
      console.error(error);
    }
  }, delayMs);
}

async function stopAll() {
  const stopState = state.globalPayload?.global?.stop_all_state;
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
      queueRefresh(150);
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
  document.getElementById("stop-all-btn").addEventListener("click", () => {
    stopAll().catch((error) => console.error(error));
  });

  document.getElementById("filter-apply").addEventListener("click", () => {
    state.page = 1;
    switchTab("table");
    queueRefresh(0);
  });

  document.getElementById("filter-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      state.page = 1;
      switchTab("table");
      queueRefresh(0);
    }
  });

  document.getElementById("page-prev").addEventListener("click", () => {
    const current = state.tablePayload?.page || 1;
    if (current <= 1) return;
    state.page = current - 1;
    queueRefresh(0);
  });

  document.getElementById("page-next").addEventListener("click", () => {
    const current = state.tablePayload?.page || 1;
    const total = state.tablePayload?.total_pages || 1;
    if (current >= total) return;
    state.page = current + 1;
    queueRefresh(0);
  });

  document.querySelector(".classification-tabs").addEventListener("click", (event) => {
    const button = event.target.closest(".classification-tab");
    if (!button) return;
    switchTab(String(button.dataset.tab || "table"));
  });
}

async function bootstrap() {
  initSoundNotifier();
  window.addEventListener("beforeunload", () => {
    teardownSoundNotifier();
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
  await refreshAll();
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  alert(error.message || String(error));
});
