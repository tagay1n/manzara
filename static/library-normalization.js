const state = {
  entityType: "personality",
  entityLabel: "Personalities",
  pageTitle: "Normalization",
  globalPayload: null,
  summaryPayload: null,
  queuePayload: null,
  canonicalsPayload: null,
  suggestionsPayload: null,
  mergePayload: null,
  qualityPayload: null,
  historyPayload: null,
  eventCursor: 0,
  eventStreamController: null,
  refreshTimer: null,
  soundNotifier: null,
  activeTab: "queue",
  queuePage: 1,
  queuePageSize: 40,
  canonicals: [],
  queueRows: [],
};

const ENTITY_LABELS = {
  personality: "Personalities",
  publisher: "Publishers",
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

function encodeKey(value) {
  return encodeURIComponent(String(value || ""));
}

function decodeKey(value) {
  return decodeURIComponent(String(value || ""));
}

function parseEntityTypeFromPath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  const maybe = parts[2] || "personality";
  return maybe === "publisher" ? "publisher" : "personality";
}

function initEntityContext() {
  state.entityType = parseEntityTypeFromPath();
  state.entityLabel = ENTITY_LABELS[state.entityType] || "Entities";
  state.pageTitle = `${state.entityLabel} Normalization`;
  document.getElementById("normalization-title").textContent = state.pageTitle;

  const link = document.getElementById("entity-source-link");
  if (state.entityType === "publisher") {
    link.href = "/library/publishers";
    link.textContent = "Publishers Page";
  } else {
    link.href = "/library/personalities";
    link.textContent = "Personalities Page";
  }
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
  window.ManzaraCore.applyStopAllButton(stopBtn, payload.global.stop_all_state);
}

function renderSummary() {
  const payload = state.summaryPayload || {};
  const dashboard = payload.dashboard || {};
  const stats = dashboard.stats || {};
  const suggestions = dashboard.suggestions || {};
  const statusNode = document.getElementById("normalization-status");

  if (dashboard.available) {
    const source = dashboard.config_source ? ` • source: ${dashboard.config_source}` : "";
    statusNode.textContent = `${state.pageTitle} loaded${source}`;
    statusNode.classList.remove("library-status-error");
  } else {
    statusNode.textContent = `Normalization unavailable: ${dashboard.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
  }

  document.getElementById("normalization-stat-grid").innerHTML = `
    <div class="library-stat-card"><span class="library-stat-label">Total Aliases</span><span class="library-stat-value">${stats.total_aliases || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Docs With Entities</span><span class="library-stat-value">${stats.docs_with_entities || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Canonicals</span><span class="library-stat-value">${stats.canonicals || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Linked</span><span class="library-stat-value">${stats.linked || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Unreviewed</span><span class="library-stat-value">${stats.unreviewed || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Suggested</span><span class="library-stat-value">${stats.suggested || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Coverage</span><span class="library-stat-value">${Number(stats.coverage_pct || 0).toFixed(2)}%</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Open Suggestions</span><span class="library-stat-value">${suggestions.open_total || 0}</span></div>
  `;
}

function canonicalOptionsHtml(selectedCanonicalId = null, includeBlank = true) {
  const options = [];
  if (includeBlank) {
    options.push('<option value="">Select canonical...</option>');
  }
  for (const canonical of state.canonicals) {
    const canonicalId = Number(canonical.canonical_id || 0);
    const selected = selectedCanonicalId && Number(selectedCanonicalId) === canonicalId ? " selected" : "";
    options.push(
      `<option value="${canonicalId}"${selected}>${canonicalId} • ${escapeHtml(
        canonical.display_name || ""
      )}</option>`
    );
  }
  return options.join("");
}

function renderCanonicals() {
  const payload = state.canonicalsPayload || {};
  const statusNode = document.getElementById("canonical-status");
  if (!payload.available) {
    statusNode.textContent = `Canonical registry unavailable: ${payload.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
    document.getElementById("canonical-table-body").innerHTML = "";
    return;
  }
  statusNode.classList.remove("library-status-error");
  statusNode.textContent = `Loaded ${payload.items.length} canonical entries`;
  state.canonicals = payload.items || [];

  document.getElementById("canonical-table-body").innerHTML = (payload.items || [])
    .map(
      (item) => `
      <tr>
        <td>${item.canonical_id || "-"}</td>
        <td>${escapeHtml(item.display_name || "-")}</td>
        <td>${escapeHtml(item.normalized_name || "-")}</td>
        <td>${item.linked_aliases || 0}</td>
        <td>${escapeHtml(item.status || "active")}</td>
        <td>${escapeHtml(item.notes || "-")}</td>
      </tr>
    `
    )
    .join("");

  document.getElementById("tab-badge-canonicals").textContent = String((payload.items || []).length);
  document.getElementById("queue-bulk-canonical").innerHTML = canonicalOptionsHtml(null, true);
}

function queueFilters() {
  return {
    search: document.getElementById("queue-filter-search").value.trim(),
    status: document.getElementById("queue-filter-status").value,
    scriptLabel: document.getElementById("queue-filter-script").value,
    minDocs: Number(document.getElementById("queue-filter-min-docs").value || "0"),
  };
}

function queueUrl() {
  const filters = queueFilters();
  const params = new URLSearchParams({
    status: filters.status,
    search: filters.search,
    script_label: filters.scriptLabel,
    min_docs: String(Math.max(0, filters.minDocs || 0)),
    page: String(state.queuePage),
    page_size: String(state.queuePageSize),
  });
  return `/api/library/normalization/${encodeURIComponent(state.entityType)}/queue?${params.toString()}`;
}

function queueSelectedRawNames() {
  return Array.from(document.querySelectorAll(".queue-row-select:checked")).map((el) =>
    decodeKey(el.dataset.raw || "")
  );
}

function clearQueueSelection() {
  document.querySelectorAll(".queue-row-select").forEach((node) => {
    node.checked = false;
  });
  document.getElementById("queue-select-all").checked = false;
}

function renderQueue() {
  const payload = state.queuePayload || {};
  const statusNode = document.getElementById("queue-status");
  if (!payload.available) {
    statusNode.textContent = `Queue unavailable: ${payload.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
    document.getElementById("queue-table-body").innerHTML = "";
    return;
  }
  statusNode.classList.remove("library-status-error");
  statusNode.textContent = `Loaded ${payload.items.length} rows from ${payload.total} total`;
  state.queueRows = payload.items || [];

  const tableBody = document.getElementById("queue-table-body");
  tableBody.innerHTML = (payload.items || [])
    .map((row) => {
      const key = encodeKey(row.raw_name || "");
      const suggestion = row.suggestion || null;
      const canonicalHint = row.canonical_name || (suggestion?.target_canonical_id ? `#${suggestion.target_canonical_id}` : "-");
      const suggestionText = suggestion
        ? `${suggestion.kind || "-"} • ${(Number(suggestion.confidence || 0) * 100).toFixed(0)}%`
        : "-";
      return `
      <tr>
        <td><input class="queue-row-select" type="checkbox" data-raw="${key}" /></td>
        <td>${escapeHtml(row.raw_name || "-")}<div class="workflow-footnote">${escapeHtml(row.normalized_name || "-")}</div></td>
        <td>${escapeHtml(row.script_label || "other")}</td>
        <td>${row.docs_count || 0}</td>
        <td>${row.mentions_count || 0}</td>
        <td>${escapeHtml(row.queue_status || "-")}</td>
        <td>
          <div>${escapeHtml(canonicalHint)}</div>
          <select class="filter-select queue-link-select" data-raw="${key}">
            ${canonicalOptionsHtml(row.canonical_id || suggestion?.target_canonical_id || null, true)}
          </select>
        </td>
        <td>${escapeHtml(suggestionText)}</td>
        <td>
          <div class="normalization-row-actions">
            <button class="small-btn queue-action-btn" data-action="link" data-raw="${key}">Link</button>
            <button class="small-btn queue-action-btn" data-action="create" data-raw="${key}">Create</button>
            <button class="small-btn queue-action-btn" data-action="reject" data-raw="${key}">Reject</button>
            <button class="small-btn queue-action-btn" data-action="evidence" data-raw="${key}">Evidence</button>
          </div>
        </td>
      </tr>
    `;
    })
    .join("");

  document.getElementById("queue-page-label").textContent = `Page ${payload.page} / ${payload.total_pages}`;
  document.getElementById("queue-page-prev").disabled = payload.page <= 1;
  document.getElementById("queue-page-next").disabled = payload.page >= payload.total_pages;
  clearQueueSelection();
}

function renderSuggestions() {
  const payload = state.suggestionsPayload || {};
  const statusNode = document.getElementById("suggestions-status");
  if (!payload.available) {
    statusNode.textContent = `Suggestions unavailable: ${payload.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
    document.getElementById("suggestions-table-body").innerHTML = "";
    return;
  }
  statusNode.classList.remove("library-status-error");
  statusNode.textContent = `Loaded ${payload.items.length} open suggestions`;
  document.getElementById("tab-badge-suggestions").textContent = String((payload.items || []).length);
  document.getElementById("suggestions-table-body").innerHTML = (payload.items || [])
    .map((row) => {
      const key = encodeKey(row.raw_name || "");
      const targetLabel = row.target_canonical_name
        ? `${row.target_canonical_id} • ${row.target_canonical_name}`
        : row.target_canonical_id
          ? `#${row.target_canonical_id}`
          : "-";
      return `
      <tr>
        <td>${escapeHtml(row.raw_name || "-")}</td>
        <td>${escapeHtml(row.suggestion_kind || "-")}</td>
        <td>${escapeHtml(targetLabel)}</td>
        <td>${(Number(row.confidence || 0) * 100).toFixed(1)}% • ${escapeHtml(row.confidence_band || "-")}</td>
        <td>${escapeHtml(row.rationale || "-")}</td>
        <td>
          <div class="normalization-row-actions">
            <button class="small-btn suggestion-action-btn" data-action="accept" data-raw="${key}" data-suggestion-id="${row.suggestion_id || 0}">Accept</button>
            <button class="small-btn suggestion-action-btn" data-action="reject" data-raw="${key}" data-suggestion-id="${row.suggestion_id || 0}">Reject</button>
            <button class="small-btn suggestion-action-btn" data-action="queue" data-raw="${key}">Open</button>
          </div>
        </td>
      </tr>
    `;
    })
    .join("");
}

function renderMergeCandidates() {
  const payload = state.mergePayload || {};
  const statusNode = document.getElementById("merge-status");
  if (!payload.available) {
    statusNode.textContent = `Merge candidates unavailable: ${payload.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
    document.getElementById("merge-root").innerHTML = "";
    return;
  }
  statusNode.classList.remove("library-status-error");
  statusNode.textContent = `Found ${payload.summary?.candidate_count || 0} merge candidates`;
  document.getElementById("merge-root").innerHTML = (payload.items || [])
    .map((item) => {
      const left = item.left || {};
      const right = item.right || {};
      const recommended = Number(item.recommended_primary_canonical_id || 0);
      const source = recommended === Number(left.canonical_id || 0) ? right : left;
      const target = recommended === Number(left.canonical_id || 0) ? left : right;
      return `
      <div class="duplicate-card">
        <div class="duplicate-head">
          <span class="duplicate-path">${escapeHtml(left.display_name || "-")} ↔ ${escapeHtml(right.display_name || "-")}</span>
          <span class="panel-pill">score ${Number(item.score || 0).toFixed(3)}</span>
        </div>
        <div class="workflow-footnote">
          Recommended: merge #${source.canonical_id || "-"} into #${target.canonical_id || "-"} • impact ${item.impact || 0}
        </div>
        <div class="normalization-row-actions">
          <button
            class="small-btn merge-apply-btn"
            data-source-id="${source.canonical_id || 0}"
            data-target-id="${target.canonical_id || 0}"
          >
            Merge Now
          </button>
        </div>
      </div>
    `;
    })
    .join("");
}

function renderQuality() {
  const quality = state.qualityPayload || {};
  const statusNode = document.getElementById("quality-status");
  if (!quality.available) {
    statusNode.textContent = `Quality unavailable: ${quality.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
    document.getElementById("quality-stat-grid").innerHTML = "";
    document.getElementById("quality-unresolved").innerHTML = "";
    return;
  }
  statusNode.classList.remove("library-status-error");
  statusNode.textContent = "Quality metrics loaded";
  const stats = quality.stats || {};
  document.getElementById("quality-stat-grid").innerHTML = `
    <div class="library-stat-card"><span class="library-stat-label">Total Aliases</span><span class="library-stat-value">${stats.total_aliases || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Linked</span><span class="library-stat-value">${stats.linked_aliases || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Rejected</span><span class="library-stat-value">${stats.rejected_aliases || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Unresolved</span><span class="library-stat-value">${stats.unresolved_aliases || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Unresolved Docs</span><span class="library-stat-value">${stats.unresolved_docs_estimate || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Duplicate Keys</span><span class="library-stat-value">${stats.duplicate_normalized_keys || 0}</span></div>
    <div class="library-stat-card"><span class="library-stat-label">Coverage</span><span class="library-stat-value">${Number(stats.coverage_pct || 0).toFixed(2)}%</span></div>
  `;

  const unresolved = (state.summaryPayload?.dashboard?.top_unresolved || []).slice(0, 16);
  document.getElementById("quality-unresolved").innerHTML =
    unresolved.length === 0
      ? '<div class="run-row">No unresolved aliases.</div>'
      : unresolved
          .map(
            (item) => `
          <div class="library-top-row">
            <div class="library-top-main">
              <span class="library-top-ddc">${escapeHtml(item.raw_name || "-")}</span>
              <span class="library-top-path">${escapeHtml(item.normalized_name || "-")} • ${escapeHtml(item.script_label || "other")}</span>
            </div>
            <span class="library-top-count">${item.docs_count || 0}</span>
          </div>
        `
          )
          .join("");
}

function historyItemDescription(item) {
  const action = String(item.action || "");
  const payload = item.payload || {};
  if (action === "link_alias") {
    return `link ${payload.raw_name || "-"} -> #${payload.canonical_id || "-"}`;
  }
  if (action === "reject_alias") {
    return `reject ${payload.raw_name || "-"}`;
  }
  if (action === "create_and_link_alias") {
    return `create+link ${payload.raw_name || "-"} -> #${payload.created_canonical_id || "-"}`;
  }
  if (action === "merge_canonicals") {
    const source = payload.source_before?.canonical_id || "-";
    const target = payload.target_before?.canonical_id || "-";
    return `merge #${source} -> #${target}`;
  }
  if (action === "refresh_suggestions") {
    return `refresh suggestions (${payload.generated || 0})`;
  }
  if (action === "bulk_link_aliases") {
    return `bulk link ${((payload.raw_names || []).length || 0)} aliases`;
  }
  if (action === "bulk_reject_aliases") {
    return `bulk reject ${((payload.raw_names || []).length || 0)} aliases`;
  }
  return action || "event";
}

function renderHistory() {
  const payload = state.historyPayload || {};
  const statusNode = document.getElementById("history-status");
  if (!payload.available) {
    statusNode.textContent = `History unavailable: ${payload.error || "unknown error"}`;
    statusNode.classList.add("library-status-error");
    document.getElementById("history-root").innerHTML = "";
    return;
  }
  statusNode.classList.remove("library-status-error");
  statusNode.textContent = `Loaded ${payload.items.length} events`;
  document.getElementById("history-root").innerHTML =
    payload.items.length === 0
      ? '<div class="run-row">No events yet.</div>'
      : payload.items
          .map(
            (item) => `
          <div class="run-row">
            <div>
              <div>#${item.event_id || "-"} • ${escapeHtml(historyItemDescription(item))}</div>
              <div class="workflow-footnote">${escapeHtml(formatDateTime(item.created_at))} • reverted: ${item.reverted ? "yes" : "no"}</div>
            </div>
            ${
              item.reverted
                ? '<span class="panel-pill">reverted</span>'
                : `<button class="small-btn history-undo-btn" data-event-id="${item.event_id || 0}">Undo</button>`
            }
          </div>
        `
          )
          .join("");
}

async function refreshSummary() {
  const payload = await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}`);
  state.summaryPayload = payload;
  state.globalPayload = payload;
  renderGlobalState(payload);
  renderSummary();
}

async function refreshCanonicals() {
  const search = document.getElementById("canonical-search").value.trim();
  const params = new URLSearchParams({ search });
  const payload = await api(
    `/api/library/normalization/${encodeURIComponent(state.entityType)}/canonicals?${params.toString()}`
  );
  state.canonicalsPayload = payload;
  renderCanonicals();
}

async function refreshQueue() {
  const payload = await api(queueUrl());
  state.queuePayload = payload;
  renderQueue();
}

async function refreshSuggestions() {
  const payload = await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/suggestions?limit=300`);
  state.suggestionsPayload = payload;
  renderSuggestions();
}

async function refreshMergeCandidates() {
  const minScore = Number(document.getElementById("merge-min-score").value || "0.84");
  const params = new URLSearchParams({
    min_score: String(Math.max(0, Math.min(1, Number.isFinite(minScore) ? minScore : 0.84))),
    limit: "120",
  });
  const payload = await api(
    `/api/library/normalization/${encodeURIComponent(state.entityType)}/merge-candidates?${params.toString()}`
  );
  state.mergePayload = payload;
  renderMergeCandidates();
}

async function refreshQuality() {
  const payload = await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/quality`);
  state.qualityPayload = payload;
  renderQuality();
}

async function refreshHistory() {
  const payload = await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/history?limit=200`);
  state.historyPayload = payload;
  renderHistory();
}

async function refreshTab(tab) {
  if (tab === "queue") {
    await refreshQueue();
    return;
  }
  if (tab === "canonicals") {
    await refreshCanonicals();
    return;
  }
  if (tab === "suggestions") {
    await refreshSuggestions();
    return;
  }
  if (tab === "merge") {
    await refreshMergeCandidates();
    return;
  }
  if (tab === "quality") {
    await refreshQuality();
    return;
  }
  if (tab === "history") {
    await refreshHistory();
  }
}

async function refreshAll() {
  await refreshSummary();
  await refreshCanonicals();
  await Promise.all([
    refreshQueue(),
    refreshSuggestions(),
    refreshMergeCandidates(),
    refreshQuality(),
    refreshHistory(),
  ]);
  applyActiveTab();
  lucide.createIcons();
}

function queueRefresh(delayMs = 250) {
  if (state.refreshTimer) return;
  state.refreshTimer = setTimeout(async () => {
    state.refreshTimer = null;
    try {
      await refreshSummary();
      await refreshTab(state.activeTab);
      lucide.createIcons();
    } catch (error) {
      console.error(error);
    }
  }, delayMs);
}

function applyActiveTab() {
  const tabs = ["queue", "canonicals", "suggestions", "merge", "quality", "history"];
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

async function switchTab(tab) {
  if (!["queue", "canonicals", "suggestions", "merge", "quality", "history"].includes(tab)) return;
  state.activeTab = tab;
  applyActiveTab();
  await refreshTab(tab);
  lucide.createIcons();
}

function queueSuggestionId(rawName) {
  const row = (state.queueRows || []).find((item) => String(item.raw_name || "") === String(rawName || ""));
  const suggestion = row?.suggestion;
  if (!suggestion || !suggestion.suggestion_id) return [];
  return [Number(suggestion.suggestion_id)];
}

function rowCanonicalSelection(rawName) {
  const key = encodeKey(rawName);
  const select = document.querySelector(`.queue-link-select[data-raw="${key}"]`);
  if (!select) return null;
  const value = Number(select.value || 0);
  return Number.isFinite(value) && value > 0 ? value : null;
}

async function linkAlias(rawName, canonicalId, suggestionIds = []) {
  if (!canonicalId) {
    alert("Select canonical first.");
    return;
  }
  await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/decisions/link`, {
    method: "POST",
    body: JSON.stringify({
      raw_name: rawName,
      canonical_id: canonicalId,
      suggestion_ids: suggestionIds,
    }),
  });
}

async function createAndLinkAlias(rawName, suggestionIds = []) {
  const displayName = window.prompt("Canonical display name:", rawName || "");
  if (displayName === null) return;
  const trimmed = displayName.trim();
  if (!trimmed) {
    alert("Display name is required.");
    return;
  }
  await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/decisions/create-link`, {
    method: "POST",
    body: JSON.stringify({
      raw_name: rawName,
      display_name: trimmed,
      suggestion_ids: suggestionIds,
    }),
  });
}

async function rejectAlias(rawName, suggestionIds = []) {
  await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/decisions/reject`, {
    method: "POST",
    body: JSON.stringify({
      raw_name: rawName,
      suggestion_ids: suggestionIds,
    }),
  });
}

async function handleQueueAction(action, rawName) {
  if (action === "link") {
    await linkAlias(rawName, rowCanonicalSelection(rawName), queueSuggestionId(rawName));
  } else if (action === "create") {
    await createAndLinkAlias(rawName, queueSuggestionId(rawName));
  } else if (action === "reject") {
    await rejectAlias(rawName, queueSuggestionId(rawName));
  } else if (action === "evidence") {
    await openEvidence(rawName);
    return;
  } else {
    return;
  }
  await Promise.all([refreshSummary(), refreshQueue(), refreshCanonicals(), refreshSuggestions(), refreshHistory(), refreshQuality()]);
  lucide.createIcons();
}

async function acceptSuggestion(rawName, suggestionId) {
  const item = (state.suggestionsPayload?.items || []).find(
    (row) => String(row.raw_name || "") === String(rawName || "") && Number(row.suggestion_id || 0) === Number(suggestionId || 0)
  );
  if (!item) return;
  if (item.suggestion_kind === "link" && Number(item.target_canonical_id || 0) > 0) {
    await linkAlias(rawName, Number(item.target_canonical_id), [Number(suggestionId)]);
    return;
  }
  if (item.suggestion_kind === "create") {
    await createAndLinkAlias(rawName, [Number(suggestionId)]);
    return;
  }
  await rejectAlias(rawName, [Number(suggestionId)]);
}

async function openEvidence(rawName) {
  const dialog = document.getElementById("evidence-dialog");
  const title = document.getElementById("evidence-title");
  const content = document.getElementById("evidence-content");
  title.textContent = `Alias Evidence: ${rawName}`;
  content.textContent = "Loading...";
  if (!dialog.open) dialog.showModal();

  try {
    const params = new URLSearchParams({
      raw_name: rawName,
      limit: "30",
    });
    const payload = await api(
      `/api/library/normalization/${encodeURIComponent(state.entityType)}/evidence?${params.toString()}`
    );
    if (!payload.available) {
      content.textContent = `Evidence unavailable: ${payload.error || "unknown error"}`;
      return;
    }
    if (!payload.items || payload.items.length === 0) {
      content.textContent = "No sample documents found for this alias.";
      return;
    }
    content.textContent = payload.items
      .map(
        (item) =>
          `md5=${item.md5 || "-"}\nlang=${item.language || "-"}\nya_path=${item.ya_path || "-"}\ndocument_url=${item.document_url || "-"}\ncontent_url=${item.content_url || "-"}\n`
      )
      .join("\n");
  } catch (error) {
    content.textContent = String(error?.message || error);
  }
}

async function refreshSuggestionsAction() {
  const limit = Number(document.getElementById("suggestions-limit").value || "120");
  const useGemini = document.getElementById("suggestions-use-gemini").checked;
  await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/suggestions/refresh`, {
    method: "POST",
    body: JSON.stringify({
      limit: Math.max(1, Math.min(1000, Number.isFinite(limit) ? limit : 120)),
      use_gemini: useGemini,
    }),
  });
  await Promise.all([refreshSummary(), refreshQueue(), refreshSuggestions(), refreshHistory(), refreshQuality()]);
}

async function applyMerge(sourceCanonicalId, targetCanonicalId) {
  const confirmed = window.confirm(`Merge canonical #${sourceCanonicalId} into #${targetCanonicalId}?`);
  if (!confirmed) return;
  await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/merge`, {
    method: "POST",
    body: JSON.stringify({
      source_canonical_id: sourceCanonicalId,
      target_canonical_id: targetCanonicalId,
      reason: "manual_merge",
    }),
  });
  await Promise.all([refreshSummary(), refreshCanonicals(), refreshQueue(), refreshMergeCandidates(), refreshHistory(), refreshQuality()]);
}

async function undoHistoryEvent(eventId) {
  const confirmed = window.confirm(`Undo event #${eventId}?`);
  if (!confirmed) return;
  await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/history/${encodeURIComponent(String(eventId))}/undo`, {
    method: "POST",
  });
  await Promise.all([refreshSummary(), refreshCanonicals(), refreshQueue(), refreshSuggestions(), refreshHistory(), refreshQuality(), refreshMergeCandidates()]);
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
      queueRefresh(150);
    },
  });
  state.eventStreamController.start();
}

function attachUiHandlers() {

  document.getElementById("stop-all-btn").addEventListener("click", () => {
    stopAll().catch((error) => console.error(error));
  });

  document.querySelectorAll(".classification-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tab = btn.getAttribute("data-tab");
      switchTab(tab).catch((error) => console.error(error));
    });
  });

  document.getElementById("queue-filter-apply").addEventListener("click", () => {
    state.queuePage = 1;
    refreshQueue().catch((error) => console.error(error));
  });

  document.getElementById("queue-page-prev").addEventListener("click", () => {
    if (state.queuePage <= 1) return;
    state.queuePage -= 1;
    refreshQueue().catch((error) => console.error(error));
  });
  document.getElementById("queue-page-next").addEventListener("click", () => {
    const totalPages = Number(state.queuePayload?.total_pages || 1);
    if (state.queuePage >= totalPages) return;
    state.queuePage += 1;
    refreshQueue().catch((error) => console.error(error));
  });

  document.getElementById("queue-select-all").addEventListener("change", (event) => {
    const checked = Boolean(event.target.checked);
    document.querySelectorAll(".queue-row-select").forEach((node) => {
      node.checked = checked;
    });
  });

  document.getElementById("queue-table-body").addEventListener("click", (event) => {
    const target = event.target.closest(".queue-action-btn");
    if (!target) return;
    const action = target.dataset.action || "";
    const rawName = decodeKey(target.dataset.raw || "");
    handleQueueAction(action, rawName).catch((error) => {
      console.error(error);
      alert(error.message || String(error));
    });
  });

  document.getElementById("queue-bulk-link").addEventListener("click", async () => {
    const canonicalId = Number(document.getElementById("queue-bulk-canonical").value || "0");
    if (!canonicalId) {
      alert("Select canonical for bulk link.");
      return;
    }
    const rawNames = queueSelectedRawNames();
    if (!rawNames.length) {
      alert("Select at least one alias row.");
      return;
    }
    await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/bulk/link`, {
      method: "POST",
      body: JSON.stringify({
        raw_names: rawNames,
        canonical_id: canonicalId,
      }),
    });
    await Promise.all([refreshSummary(), refreshQueue(), refreshCanonicals(), refreshSuggestions(), refreshHistory(), refreshQuality()]);
  });

  document.getElementById("queue-bulk-reject").addEventListener("click", async () => {
    const rawNames = queueSelectedRawNames();
    if (!rawNames.length) {
      alert("Select at least one alias row.");
      return;
    }
    await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/bulk/reject`, {
      method: "POST",
      body: JSON.stringify({
        raw_names: rawNames,
      }),
    });
    await Promise.all([refreshSummary(), refreshQueue(), refreshCanonicals(), refreshSuggestions(), refreshHistory(), refreshQuality()]);
  });

  document.getElementById("queue-clear-selection").addEventListener("click", () => {
    clearQueueSelection();
  });

  document.getElementById("canonical-create-btn").addEventListener("click", async () => {
    const displayName = document.getElementById("canonical-create-name").value.trim();
    const notes = document.getElementById("canonical-create-notes").value.trim();
    if (!displayName) {
      alert("Canonical display name is required.");
      return;
    }
    await api(`/api/library/normalization/${encodeURIComponent(state.entityType)}/canonicals`, {
      method: "POST",
      body: JSON.stringify({
        display_name: displayName,
        notes,
      }),
    });
    document.getElementById("canonical-create-name").value = "";
    await Promise.all([refreshSummary(), refreshCanonicals(), refreshQueue(), refreshMergeCandidates(), refreshHistory()]);
  });

  document.getElementById("canonical-search-apply").addEventListener("click", () => {
    refreshCanonicals().catch((error) => console.error(error));
  });

  document.getElementById("suggestions-refresh-btn").addEventListener("click", () => {
    refreshSuggestionsAction()
      .then(() => {
        lucide.createIcons();
      })
      .catch((error) => {
        console.error(error);
        alert(error.message || String(error));
      });
  });

  document.getElementById("suggestions-table-body").addEventListener("click", (event) => {
    const target = event.target.closest(".suggestion-action-btn");
    if (!target) return;
    const action = target.dataset.action || "";
    const rawName = decodeKey(target.dataset.raw || "");
    const suggestionId = Number(target.dataset.suggestionId || 0);
    (async () => {
      if (action === "accept") {
        await acceptSuggestion(rawName, suggestionId);
      } else if (action === "reject") {
        await rejectAlias(rawName, [suggestionId]);
      } else if (action === "queue") {
        document.getElementById("queue-filter-search").value = rawName;
        state.queuePage = 1;
        await switchTab("queue");
        await refreshQueue();
      }
      await Promise.all([refreshSummary(), refreshSuggestions(), refreshQueue(), refreshHistory(), refreshQuality(), refreshCanonicals()]);
      lucide.createIcons();
    })().catch((error) => {
      console.error(error);
      alert(error.message || String(error));
    });
  });

  document.getElementById("merge-load-btn").addEventListener("click", () => {
    refreshMergeCandidates().catch((error) => console.error(error));
  });
  document.getElementById("merge-root").addEventListener("click", (event) => {
    const button = event.target.closest(".merge-apply-btn");
    if (!button) return;
    const sourceId = Number(button.dataset.sourceId || 0);
    const targetId = Number(button.dataset.targetId || 0);
    if (!sourceId || !targetId) return;
    applyMerge(sourceId, targetId).catch((error) => {
      console.error(error);
      alert(error.message || String(error));
    });
  });

  document.getElementById("history-root").addEventListener("click", (event) => {
    const button = event.target.closest(".history-undo-btn");
    if (!button) return;
    const eventId = Number(button.dataset.eventId || 0);
    if (!eventId) return;
    undoHistoryEvent(eventId).catch((error) => {
      console.error(error);
      alert(error.message || String(error));
    });
  });

  const evidenceDialog = document.getElementById("evidence-dialog");
  const closeEvidence = () => {
    if (evidenceDialog.open) evidenceDialog.close();
  };
  document.getElementById("evidence-close-btn").addEventListener("click", closeEvidence);
  document.getElementById("evidence-close-footer-btn").addEventListener("click", closeEvidence);
}

async function bootstrap() {
  initEntityContext();
  initSoundNotifier();
  window.addEventListener("beforeunload", () => {
    teardownSoundNotifier();
    if (state.eventStreamController) {
      state.eventStreamController.stop();
      state.eventStreamController = null;
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

