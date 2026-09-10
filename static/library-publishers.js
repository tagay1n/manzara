"use strict";

const PUBLISHER_TASK_ID = "library.publisher_suggestions_refresh";
const ACTIVE_TASK_STATES = new Set(["starting", "running", "stopping_graceful", "stopping_force"]);
const UNDOABLE_ACTIONS = new Set([
  "link_alias", "reject_alias", "create_and_link_alias", "create_canonical_group",
  "bulk_link_aliases", "bulk_reject_aliases", "merge_canonicals", "rename_canonical",
]);

const state = {
  page: 1,
  pageSize: 40,
  activeTab: "review",
  summary: null,
  queue: null,
  canonicals: [],
  history: [],
  task: null,
  selected: new Map(),
  eventCursor: 0,
  eventStreamController: null,
};

const api = (path, options = {}) => window.ManzaraCore.api(path, options);
const escapeHtml = (value) => window.ManzaraCore.escapeHtml(value);
const encodeKey = (value) => encodeURIComponent(String(value || ""));
const decodeKey = (value) => decodeURIComponent(String(value || ""));

function filters() {
  return {
    search: document.getElementById("filter-search").value.trim(),
    status: document.getElementById("filter-status").value,
    script: document.getElementById("filter-script").value,
    minDocs: Math.max(0, Number(document.getElementById("filter-min-docs").value || 0)),
  };
}

function queueUrl() {
  const value = filters();
  const params = new URLSearchParams({
    page: String(state.page), page_size: String(state.pageSize), search: value.search,
    status: value.status, script_label: value.script, min_docs: String(value.minDocs),
  });
  return `/api/library/normalization/publisher/queue?${params.toString()}`;
}

function renderSummary() {
  const dashboard = state.summary?.dashboard || {};
  const stats = dashboard.stats || {};
  const status = document.getElementById("publisher-status");
  status.textContent = dashboard.available
    ? "Choose spellings that belong to the same publisher. Nothing changes until you confirm."
    : `Publishers unavailable: ${dashboard.error || "unknown error"}`;
  status.classList.toggle("library-status-error", !dashboard.available);
  document.getElementById("publisher-stat-grid").innerHTML = `
    <div class="library-stat-card"><span class="library-stat-label">Raw names</span><span class="library-stat-value">${stats.total_aliases || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Publisher entities</span><span class="library-stat-value">${stats.canonicals || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Linked aliases</span><span class="library-stat-value">${stats.linked || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Needs review</span><span class="library-stat-value">${(stats.unreviewed || 0) + (stats.suggested || 0)}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Coverage</span><span class="library-stat-value">${Number(stats.coverage_pct || 0).toFixed(1)}%</span></div>`;
  document.getElementById("tab-badge-review").textContent = String((stats.unreviewed || 0) + (stats.suggested || 0));
  document.getElementById("tab-badge-publishers").textContent = String(stats.canonicals || 0);
}

function suggestionLabel(row) {
  const suggestion = row.suggestion;
  if (!suggestion) return '<span class="workflow-footnote">No suggestion</span>';
  const confidence = `${Math.round(Number(suggestion.confidence || 0) * 100)}%`;
  if (suggestion.kind === "link") {
    return `<strong>${escapeHtml(row.canonical_name || `Publisher #${suggestion.target_canonical_id}`)}</strong><div class="workflow-footnote">Suggested match · ${confidence}</div>`;
  }
  if (suggestion.kind === "create") return `<strong>New publisher</strong><div class="workflow-footnote">${confidence} · ${escapeHtml(suggestion.rationale || "")}</div>`;
  return `<strong>Possibly invalid</strong><div class="workflow-footnote">${confidence} · ${escapeHtml(suggestion.rationale || "")}</div>`;
}

function renderQueue() {
  const payload = state.queue || {};
  const status = document.getElementById("publisher-table-status");
  if (!payload.available) {
    status.textContent = `Names unavailable: ${payload.error || "unknown error"}`;
    status.classList.add("library-status-error");
    document.getElementById("publisher-table-body").innerHTML = "";
    return;
  }
  status.classList.remove("library-status-error");
  status.textContent = `${payload.total || 0} publisher spellings · selections stay while you search or change pages`;
  document.getElementById("publisher-table-body").innerHTML = (payload.items || []).map((row) => {
    const raw = String(row.raw_name || "");
    const key = encodeKey(raw);
    const checked = state.selected.has(raw) ? " checked" : "";
    const linked = row.queue_status === "linked";
    const statusLabel = linked ? `Linked to ${row.canonical_name || `#${row.canonical_id}`}` : row.queue_status === "rejected" ? "Marked invalid" : row.queue_status === "suggested" ? "Suggested" : "Unreviewed";
    return `<tr>
      <td><input class="publisher-row-select" type="checkbox" data-raw="${key}"${checked}${linked ? " disabled" : ""} aria-label="Select ${escapeHtml(raw)}" /></td>
      <td><strong>${escapeHtml(raw || "-")}</strong><div class="workflow-footnote">${escapeHtml(row.normalized_name || "-")} · ${escapeHtml(row.script_label || "other")}</div></td>
      <td>${row.docs_count || 0}<div class="workflow-footnote">${row.mentions_count || 0} mentions</div></td>
      <td>${escapeHtml(statusLabel)}</td><td>${suggestionLabel(row)}</td>
      <td><div class="normalization-row-actions">
        ${row.suggestion?.kind === "link" ? `<button class="small-btn publisher-row-action" data-action="accept" data-raw="${key}">Accept match</button>` : ""}
        ${row.suggestion?.kind === "create" ? `<button class="small-btn publisher-row-action" data-action="group" data-raw="${key}">Use in new group</button>` : ""}
        ${row.suggestion ? `<button class="small-btn publisher-row-action" data-action="dismiss" data-raw="${key}">Dismiss suggestion</button>` : ""}
        ${!linked ? `<button class="small-btn publisher-row-action" data-action="invalid" data-raw="${key}">Mark invalid</button>` : ""}
        <button class="small-btn publisher-row-action" data-action="evidence" data-raw="${key}">Evidence</button>
      </div></td></tr>`;
  }).join("") || '<tr><td colspan="6">No publisher names match these filters.</td></tr>';
  window.ManzaraCore.applyPaginationControls({
    labelNode: document.getElementById("page-label"), prevNode: document.getElementById("page-prev"),
    nextNode: document.getElementById("page-next"), page: payload.page || 1, totalPages: payload.total_pages || 1,
  });
  const visibleSelectable = (payload.items || []).filter((row) => row.queue_status !== "linked");
  document.getElementById("publisher-select-page").checked = visibleSelectable.length > 0 && visibleSelectable.every((row) => state.selected.has(String(row.raw_name || "")));
  renderSelection();
}

function renderSelection() {
  const bar = document.getElementById("publisher-selection-bar");
  const rows = Array.from(state.selected.values());
  bar.hidden = rows.length === 0;
  document.getElementById("publisher-selection-count").textContent = `${rows.length} ${rows.length === 1 ? "name" : "names"} selected`;
  document.getElementById("publisher-selection-preview").textContent = rows.slice(0, 4).map((row) => row.raw_name).join(" · ") + (rows.length > 4 ? ` · +${rows.length - 4} more` : "");
}

function canonicalOptions() {
  const query = document.getElementById("publisher-existing-search").value.trim().toLocaleLowerCase();
  const matches = state.canonicals.filter((item) => !query || String(item.display_name || "").toLocaleLowerCase().includes(query));
  const emptyLabel = state.canonicals.length ? "Add to existing…" : "No publishers yet — create one";
  return `<option value="">${emptyLabel}</option>` + matches.map((item) => `<option value="${Number(item.canonical_id || 0)}">${escapeHtml(item.display_name || "-")} (${item.linked_aliases || 0} aliases)</option>`).join("");
}

function renderCanonicals() {
  document.getElementById("publisher-existing-canonical").innerHTML = canonicalOptions();
  document.getElementById("publisher-existing-canonical").disabled = state.canonicals.length === 0;
  document.getElementById("publisher-add-existing").disabled = state.canonicals.length === 0;
  document.getElementById("canonical-status").textContent = state.canonicals.length
    ? `${state.canonicals.length} publisher entities. Open one to see every retained spelling.`
    : "No publisher entities yet. Select raw names in Review names and create the first publisher.";
  document.getElementById("canonical-root").innerHTML = state.canonicals.map((item) => `<article class="duplicate-card publisher-canonical-card" data-canonical-id="${item.canonical_id}">
    <div class="duplicate-head"><button class="publisher-canonical-open" data-canonical-id="${item.canonical_id}"><strong>${escapeHtml(item.display_name || "-")}</strong></button><span class="panel-pill">${item.linked_aliases || 0} aliases</span></div>
    <div class="normalization-row-actions"><button class="small-btn publisher-canonical-rename" data-canonical-id="${item.canonical_id}" data-name="${encodeKey(item.display_name || "")}">Rename</button></div>
    <div class="publisher-canonical-aliases" id="canonical-aliases-${item.canonical_id}" hidden></div>
  </article>`).join("") || '<div class="run-row">Create your first publisher from selected raw names.</div>';
}

function renderHistory() {
  document.getElementById("history-status").textContent = `${state.history.length} recent actions`;
  document.getElementById("history-root").innerHTML = state.history.map((item) => `<div class="run-row"><div><strong>${escapeHtml(String(item.action || "action").replaceAll("_", " "))}</strong><div class="workflow-footnote">${escapeHtml(window.ManzaraCore.formatDateTime(item.created_at))}</div></div>${!item.reverted && UNDOABLE_ACTIONS.has(String(item.action || "")) ? `<button class="small-btn publisher-history-undo" data-event-id="${item.event_id}">Undo</button>` : `<span class="panel-pill">${item.reverted ? "reverted" : "recorded"}</span>`}</div>`).join("") || '<div class="run-row">No publisher decisions yet.</div>';
}

function renderTask() {
  const run = state.task?.runs?.[0] || null;
  const runStatus = String(run?.status || "idle");
  const active = ACTIVE_TASK_STATES.has(runStatus);
  document.getElementById("publisher-match-status").textContent = active ? `Finding matches · ${runStatus.replaceAll("_", " ")}` : run ? `Last run: ${runStatus}` : "No match run yet";
  const button = document.getElementById("publisher-match-btn");
  button.textContent = active ? "Stop finding matches" : "Find matches";
  button.classList.toggle("active", active);
}

async function loadAll() {
  const search = document.getElementById("canonical-search").value.trim();
  const [summary, queue, canonicals, history, task] = await Promise.all([
    api("/api/library/normalization/publisher"), api(queueUrl()),
    api(`/api/library/normalization/publisher/canonicals?search=${encodeURIComponent(search)}`),
    api("/api/library/normalization/publisher/history?limit=100"),
    api(`/api/tasks/${encodeURIComponent(PUBLISHER_TASK_ID)}?limit=1`),
  ]);
  state.summary = summary; state.queue = queue; state.canonicals = canonicals.items || [];
  state.history = history.items || []; state.task = task;
  renderSummary(); renderQueue(); renderCanonicals(); renderHistory(); renderTask(); lucide.createIcons();
}

function selectedRows() { return Array.from(state.selected.values()); }
function selectedSuggestionIds() { return selectedRows().map((row) => Number(row.suggestion?.suggestion_id || 0)).filter((value) => value > 0); }

async function createGroup() {
  const rows = selectedRows();
  if (!rows.length) return;
  const defaultRow = [...rows].sort((a, b) => Number(b.docs_count || 0) - Number(a.docs_count || 0))[0];
  const displayName = await window.ManzaraUI.prompt({ title: "Create publisher", message: "Choose the canonical display name. Every selected spelling will be kept as an alias.", inputLabel: "Canonical name", value: defaultRow.raw_name || "", acceptLabel: "Review group" });
  if (displayName === null || !displayName.trim()) return;
  const confirmed = await window.ManzaraUI.confirm({ title: `Create ${displayName.trim()}?`, message: `Link ${rows.length} retained ${rows.length === 1 ? "alias" : "aliases"} to this publisher.`, acceptLabel: "Create publisher" });
  if (!confirmed) return;
  await api("/api/library/normalization/publisher/groups", { method: "POST", body: JSON.stringify({ display_name: displayName.trim(), raw_names: rows.map((row) => row.raw_name), suggestion_ids: selectedSuggestionIds() }) });
  state.selected.clear(); await loadAll();
}

async function addToExisting() {
  const canonicalId = Number(document.getElementById("publisher-existing-canonical").value || 0);
  const rows = selectedRows();
  if (!canonicalId || !rows.length) { window.ManzaraUI.toast("Choose an existing publisher and at least one alias.", { tone: "warning" }); return; }
  const canonical = state.canonicals.find((item) => Number(item.canonical_id) === canonicalId);
  const confirmed = await window.ManzaraUI.confirm({ title: `Add aliases to ${canonical?.display_name || "publisher"}?`, message: `${rows.length} original spellings will remain available as aliases.`, acceptLabel: "Add aliases" });
  if (!confirmed) return;
  await api("/api/library/normalization/publisher/bulk/link", { method: "POST", body: JSON.stringify({ canonical_id: canonicalId, raw_names: rows.map((row) => row.raw_name), suggestion_ids: selectedSuggestionIds() }) });
  state.selected.clear(); await loadAll();
}

async function rowAction(action, rawName) {
  const row = (state.queue?.items || []).find((item) => String(item.raw_name || "") === rawName);
  if (!row) return;
  if (action === "accept") {
    await api("/api/library/normalization/publisher/decisions/link", { method: "POST", body: JSON.stringify({ raw_name: rawName, canonical_id: Number(row.suggestion.target_canonical_id), suggestion_ids: [Number(row.suggestion.suggestion_id)] }) });
  } else if (action === "group") {
    state.selected.set(rawName, row);
    renderQueue();
    return;
  } else if (action === "dismiss") {
    await api(`/api/library/normalization/publisher/suggestions/${Number(row.suggestion.suggestion_id)}/dismiss`, { method: "POST" });
  } else if (action === "invalid") {
    const confirmed = await window.ManzaraUI.confirm({ title: "Mark publisher spelling invalid?", message: `${rawName} will leave the review queue. This is different from dismissing an automatic suggestion.`, acceptLabel: "Mark invalid", destructive: true });
    if (!confirmed) return;
    await api("/api/library/normalization/publisher/decisions/reject", { method: "POST", body: JSON.stringify({ raw_name: rawName, suggestion_ids: row.suggestion ? [Number(row.suggestion.suggestion_id)] : [] }) });
  } else if (action === "evidence") {
    const dialog = document.getElementById("publisher-evidence-dialog");
    document.getElementById("publisher-evidence-title").textContent = `Evidence: ${rawName}`;
    document.getElementById("publisher-evidence-content").textContent = "Loading…"; dialog.showModal();
    const evidence = await api(`/api/library/normalization/publisher/evidence?raw_name=${encodeURIComponent(rawName)}&limit=30`);
    document.getElementById("publisher-evidence-content").textContent = (evidence.items || []).map((item) => `md5=${item.md5 || "-"}\npath=${item.ya_path || "-"}\nurl=${item.document_url || item.content_url || "-"}`).join("\n\n") || "No document evidence found.";
    return;
  }
  await loadAll();
}

async function openCanonical(canonicalId) {
  const root = document.getElementById(`canonical-aliases-${canonicalId}`);
  if (!root.hidden) { root.hidden = true; return; }
  root.hidden = false; root.textContent = "Loading aliases…";
  const payload = await api(`/api/library/normalization/publisher/canonicals/${canonicalId}/aliases`);
  root.innerHTML = (payload.items || []).map((item) => `<div class="duplicate-item"><span>${escapeHtml(item.raw_name || "-")} · ${escapeHtml(item.script_label || "other")}</span><span>${item.docs_count || 0} docs</span></div>`).join("") || '<div class="workflow-footnote">No linked aliases.</div>';
}

async function renameCanonical(canonicalId, currentName) {
  const displayName = await window.ManzaraUI.prompt({ title: "Rename publisher", message: "Aliases and original metadata will not change.", inputLabel: "Canonical name", value: currentName, acceptLabel: "Rename" });
  if (displayName === null || !displayName.trim() || displayName.trim() === currentName) return;
  await api(`/api/library/normalization/publisher/canonicals/${canonicalId}`, { method: "PATCH", body: JSON.stringify({ display_name: displayName.trim() }) });
  await loadAll();
}

function selectTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".classification-tab").forEach((button) => { const active = button.dataset.tab === tab; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); });
  document.querySelectorAll(".classification-tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-panel-${tab}`));
}

function attachHandlers() {
  document.querySelector(".classification-tabs").addEventListener("click", (event) => { const button = event.target.closest(".classification-tab"); if (button) selectTab(button.dataset.tab); });
  document.getElementById("filter-apply").addEventListener("click", () => { state.page = 1; loadAll().catch(showError); });
  document.getElementById("filter-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { state.page = 1; loadAll().catch(showError); } });
  document.getElementById("page-prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; loadAll().catch(showError); } });
  document.getElementById("page-next").addEventListener("click", () => { if (state.page < Number(state.queue?.total_pages || 1)) { state.page += 1; loadAll().catch(showError); } });
  document.getElementById("publisher-table-body").addEventListener("change", (event) => { const input = event.target.closest(".publisher-row-select"); if (!input) return; const raw = decodeKey(input.dataset.raw); const row = (state.queue?.items || []).find((item) => String(item.raw_name || "") === raw); if (input.checked && row) state.selected.set(raw, row); else state.selected.delete(raw); renderSelection(); });
  document.getElementById("publisher-select-page").addEventListener("change", (event) => { for (const row of state.queue?.items || []) { if (row.queue_status === "linked") continue; if (event.target.checked) state.selected.set(String(row.raw_name || ""), row); else state.selected.delete(String(row.raw_name || "")); } renderQueue(); });
  document.getElementById("publisher-clear-selection").addEventListener("click", () => { state.selected.clear(); renderQueue(); });
  document.getElementById("publisher-create-group").addEventListener("click", () => createGroup().catch(showError));
  document.getElementById("publisher-add-existing").addEventListener("click", () => addToExisting().catch(showError));
  document.getElementById("publisher-existing-search").addEventListener("input", () => {
    document.getElementById("publisher-existing-canonical").innerHTML = canonicalOptions();
  });
  document.getElementById("publisher-table-body").addEventListener("click", (event) => { const button = event.target.closest(".publisher-row-action"); if (button) rowAction(button.dataset.action, decodeKey(button.dataset.raw)).catch(showError); });
  document.getElementById("canonical-search-apply").addEventListener("click", () => loadAll().catch(showError));
  document.getElementById("canonical-root").addEventListener("click", (event) => { const open = event.target.closest(".publisher-canonical-open"); const rename = event.target.closest(".publisher-canonical-rename"); if (open) openCanonical(Number(open.dataset.canonicalId)).catch(showError); if (rename) renameCanonical(Number(rename.dataset.canonicalId), decodeKey(rename.dataset.name)).catch(showError); });
  document.getElementById("history-root").addEventListener("click", (event) => { const button = event.target.closest(".publisher-history-undo"); if (button) api(`/api/library/normalization/publisher/history/${Number(button.dataset.eventId)}/undo`, { method: "POST" }).then(loadAll).catch(showError); });
  document.getElementById("publisher-evidence-close").addEventListener("click", () => document.getElementById("publisher-evidence-dialog").close());
  document.getElementById("publisher-match-btn").addEventListener("click", async () => { await api(`/api/tasks/${encodeURIComponent(PUBLISHER_TASK_ID)}/toggle`, { method: "POST", body: JSON.stringify({}) }); await loadAll(); });
}

function showError(error) {
  const message = error.message || String(error);
  console.error(error);
  document.getElementById("publisher-status").textContent = `Publishers unavailable: ${message}`;
  document.getElementById("publisher-status").classList.add("library-status-error");
  document.getElementById("publisher-table-status").textContent = `Names unavailable: ${message}`;
  document.getElementById("publisher-table-status").classList.add("library-status-error");
  window.ManzaraUI.toast(message, { tone: "error" });
}

async function bootstrap() {
  attachHandlers(); await loadAll();
  state.eventStreamController = window.ManzaraCore.createSseController({
    eventTypes: window.ManzaraCore.DEFAULT_EVENT_TYPES,
    initialCursor: window.ManzaraCore.eventCursorFromSnapshot(state.summary),
    getCursor: () => state.eventCursor, setCursor: (value) => { state.eventCursor = Number(value || 0); },
    onEvent(payload) { const type = String(payload?.type || ""); if (type.startsWith("library.") || ["task.completed", "task.failed", "task.stopped"].includes(type)) window.ManzaraCore.scheduleRefresh(state, loadAll, 200); },
  });
  state.eventStreamController.start();
  window.addEventListener("beforeunload", () => state.eventStreamController?.stop());
}

bootstrap().catch(showError);
