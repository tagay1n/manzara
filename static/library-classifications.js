const state = {
  tablePayload: null, insightsPayload: null, globalPayload: null,
  page: 1, pageSize: 25, activeTab: "tree", refreshTimer: null,
  eventCursor: 0, eventStreamController: null,
  base: new Map(), draft: new Map(), merges: new Map(),
  history: [], future: [], expanded: new Set(), documentPages: new Map(),
  documentDownloads: new Map(), dragged: null,
  editingPath: null, mergeSource: null, documentObserver: null, applying: false,
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

const tabController = window.ManzaraCore.createTabController({
  tabs: ["table", "tree", "distribution"],
  getActiveTab: () => state.activeTab,
  setActiveTab: (tab) => { state.activeTab = tab; },
});
const api = (path, options = {}) => window.ManzaraCore.api(path, options);
const escapeHtml = (value) => window.ManzaraCore.escapeHtml(value);
const toInt = (value, fallback = 0) => Number.isFinite(Number(value)) ? Math.trunc(Number(value)) : fallback;
const pathKey = (path) => JSON.stringify(path);
const encodedPath = (path) => encodeURIComponent(pathKey(path));
const samePath = (left, right) => pathKey(left) === pathKey(right);
const isPrefix = (prefix, path) => prefix.length <= path.length && prefix.every((part, i) => part === path[i]);

function tableUrl() {
  const params = new URLSearchParams({
    page: String(state.page), page_size: String(state.pageSize),
    search: document.getElementById("filter-search").value.trim(),
    ddc_prefix: document.getElementById("filter-ddc-prefix").value.trim(),
    min_usage: String(Math.max(0, Number(document.getElementById("filter-min-usage").value || 0))),
    status: document.getElementById("filter-status").value,
    sort: document.getElementById("filter-sort").value,
  });
  return `/api/library/classifications?${params}`;
}

function renderTable(payload) {
  state.tablePayload = payload;
  const status = document.getElementById("classification-table-status");
  status.classList.toggle("library-status-error", !payload.available);
  status.textContent = payload.available
    ? `Loaded ${(payload.items || []).length} rows from ${payload.total || 0} total`
    : `Table unavailable: ${payload.error || "unknown error"}`;
  document.getElementById("classification-table-body").innerHTML = payload.available ? (payload.items || []).map((item) => `
    <tr><td>${escapeHtml(item.classification_id ?? "-")}</td>
    <td><a class="run-task-link" href="/library/classifications/${encodeURIComponent(item.classification_id)}">${escapeHtml(item.ddc || "-")}</a></td>
    <td>${escapeHtml(item.path || "-")}</td><td>${escapeHtml(item.usage_count || 0)}</td>
    <td>${escapeHtml(item.status || "-")}</td><td>${escapeHtml(item.created_by || "-")}</td>
    <td>${escapeHtml(window.ManzaraCore.formatDateTime(item.created_at))}</td></tr>`).join("") : "";
  window.ManzaraCore.applyPaginationControls({
    page: payload.page, totalPages: payload.total_pages,
    labelNode: document.getElementById("page-label"),
    prevNode: document.getElementById("page-prev"), nextNode: document.getElementById("page-next"),
  });
}

function flattenTree(nodes, prefix = [], rows = []) {
  for (const node of nodes || []) {
    const path = Array.isArray(node.path) ? node.path : [...prefix, String(node.name || "")];
    for (const item of node.classifications || []) {
      const id = toInt(item.classification_id);
      if (id > 0) rows.push({ id, ddc: String(item.ddc || ""), usage: toInt(item.usage_count), path: [...path] });
    }
    flattenTree(node.children, path, rows);
  }
  return rows;
}
function expandedPaths(rows) {
  const keys = new Set();
  for (const row of rows) for (let depth = 1; depth <= row.path.length; depth += 1) keys.add(pathKey(row.path.slice(0, depth)));
  return keys;
}

function initializeDraft(payload) {
  state.base = new Map(flattenTree(payload.tree).map((row) => [row.id, row]));
  state.draft = new Map([...state.base].map(([id, row]) => [id, { ...row, path: [...row.path] }]));
  state.merges = new Map(); state.history = []; state.future = []; state.documentPages = new Map();
  state.expanded = expandedPaths(state.draft.values());
}

function snapshot() {
  return {
    draft: [...state.draft].map(([id, row]) => [id, { ...row, path: [...row.path] }]),
    merges: [...state.merges], expanded: [...state.expanded],
  };
}

function restore(value) {
  state.draft = new Map(value.draft.map(([id, row]) => [id, { ...row, path: [...row.path] }]));
  state.merges = new Map(value.merges); state.expanded = new Set(value.expanded || []);
  state.documentPages = new Map(); renderEditor();
}

function stage(mutator) {
  const before = snapshot(); mutator(); state.history.push(before); state.future = [];
  state.documentPages = new Map(); renderEditor();
}

function draftRows() { return [...state.draft.values()].filter((row) => !state.merges.has(row.id)); }

function stagedPayload() {
  const changes = [];
  for (const row of draftRows()) {
    const base = state.base.get(row.id);
    if (base && !samePath(row.path, base.path)) changes.push({ classification_id: row.id, path: row.path });
  }
  return {
    base_revision: state.insightsPayload.revision, changes,
    merges: [...state.merges].map(([source, target]) => ({ source_classification_id: source, target_classification_id: target })),
  };
}

function changeCount() { const draft = stagedPayload(); return draft.changes.length + draft.merges.length; }

function buildDraftTree() {
  const root = [];
  for (const baseRow of draftRows().sort((a, b) => a.path.join("\0").localeCompare(b.path.join("\0")) || a.ddc.localeCompare(b.ddc))) {
    const mergedUsage = [...state.merges].reduce((sum, [source, target]) => target === baseRow.id ? sum + (state.draft.get(source)?.usage || 0) : sum, 0);
    const row = { ...baseRow, path: [...baseRow.path], usage: baseRow.usage + mergedUsage };
    let nodes = root; let current;
    row.path.forEach((name, index) => {
      current = nodes.find((item) => item.name === name);
      if (!current) { current = { name, path: row.path.slice(0, index + 1), children: [], classifications: [] }; nodes.push(current); }
      nodes = current.children;
    });
    current.classifications.push(row);
  }
  const populate = (node) => {
    node.children.forEach(populate);
    node.count = node.classifications.reduce((sum, row) => sum + row.usage, 0) + node.children.reduce((sum, child) => sum + child.count, 0);
  };
  root.forEach(populate); return root;
}

function terminalIds(path) { return draftRows().filter((row) => samePath(row.path, path)).map((row) => row.id); }
function subtreeIds(path) { return draftRows().filter((row) => isPrefix(path, row.path)).map((row) => row.id); }
function documentGroup(path) {
  const ids = terminalIds(path); const sources = [...ids];
  for (const [source, target] of state.merges) if (ids.includes(target)) sources.push(source);
  return [...new Set(sources)].sort((a, b) => a - b);
}
function documentState(path) {
  const key = pathKey(path);
  if (!state.documentPages.has(key)) state.documentPages.set(key, { items: [], total: 0, hasMore: false, loading: false, error: "" });
  return state.documentPages.get(key);
}

function renderDocuments(path) {
  if (!terminalIds(path).length) return "";
  const page = documentState(path);
  if (!page.items.length && !page.loading && !page.error) return `<button class="document-load-btn small-btn" data-auto-document-path="${encodedPath(path)}" data-path="${encodedPath(path)}">Load first 10 documents</button>`;
  const items = page.items.map((doc) => {
    const download = state.documentDownloads.get(doc.md5) || {};
    const details = [
      doc.source_name, doc.language, doc.mime_type,
      doc.page_count ? `${doc.page_count} pages` : "",
      doc.full === true ? "full document" : doc.full === false ? "partial" : "",
      doc.sharing_restricted === true ? "restricted" : "",
    ].filter(Boolean).join(" · ");
    return `<li class="document-leaf"><div><strong>${escapeHtml(doc.title || doc.source_name || doc.md5)}</strong>
      <div class="workflow-footnote">${escapeHtml(details)}</div></div>
      <button class="small-btn document-open-btn" data-md5="${escapeHtml(doc.md5)}" ${download.loading ? "disabled" : ""}>${download.loading ? '<span class="inline-spinner"></span> Downloading…' : "Open locally"}</button>
      ${download.error ? `<div class="workflow-footnote library-status-error">${escapeHtml(download.error)}</div>` : ""}</li>`;
  }).join("");
  return `<ul class="document-list">${items}</ul>
    ${page.loading ? '<div class="workflow-footnote"><span class="inline-spinner"></span> Loading documents…</div>' : ""}
    ${page.error ? `<div class="workflow-footnote library-status-error">${escapeHtml(page.error)}</div>` : ""}
    ${page.hasMore ? `<button class="document-more-btn small-btn" data-path="${encodedPath(path)}">Load 10 more</button>` : ""}`;
}

function renderTreeNodes(nodes, depth = 0) {
  if (!nodes.length) return depth ? "" : '<div class="workflow-footnote">No hierarchy data.</div>';
  return `<ul class="tree-list ${depth === 0 ? "root" : "tree-children"}">${nodes.map((node) => {
    const key = pathKey(node.path); const expanded = state.expanded.has(key);
    const editing = state.editingPath === key;
    const encoded = encodedPath(node.path);
    return `<li class="tree-node"><div class="tree-row taxonomy-drop-target" draggable="${!editing}" data-drag-path="${encoded}" data-drop-path="${encoded}">
      ${editing ? `<span class="taxonomy-inline-editor"><input class="filter-input taxonomy-name-input" value="${escapeHtml(node.name)}" aria-label="Category name"><button class="small-btn" data-action="save-name" data-path="${encoded}">Save</button><button class="small-btn" data-action="cancel-name" data-path="${encoded}">Cancel</button></span>` : `<button class="tree-toggle" data-tree-path="${encoded}" aria-expanded="${expanded}"><span class="tree-caret">&#9656;</span><span class="tree-name">${escapeHtml(node.name)}</span><span class="tree-count">${node.count}</span></button>`}
      <span class="taxonomy-node-actions"><button class="icon-btn" data-action="edit-name" data-path="${encoded}">Edit</button>
      <button class="icon-btn" data-action="add-above" data-path="${encoded}" title="Insert level above">+↑</button>
      <button class="icon-btn" data-action="add-below" data-path="${encoded}" title="Insert level below">+↓</button>
      <button class="icon-btn danger" data-action="remove" data-path="${encoded}">Remove</button></span></div>
      ${expanded ? `<div class="tree-branch">${node.classifications.length ? `<div class="classification-pills">${node.classifications.map((row) => `<button class="classification-pill ${state.mergeSource === row.id ? "is-selected" : ""}" draggable="true" data-drag-classification="${row.id}" data-drop-classification="${row.id}" title="Drag onto another DDC, or select source then target, to merge"><span>${escapeHtml(row.ddc || "No DDC")}</span><span class="tree-count">${row.usage}</span></button>`).join("")}</div>` : ""}${renderDocuments(node.path)}${renderTreeNodes(node.children, depth + 1)}</div>` : ""}</li>`;
  }).join("")}</ul>`;
}

function renderChangeTray() {
  const tray = document.getElementById("taxonomy-change-tray"); const payload = stagedPayload();
  const total = payload.changes.length + payload.merges.length; tray.hidden = total === 0;
  tray.innerHTML = total ? `<strong>${total} staged change${total === 1 ? "" : "s"}</strong><span>${payload.changes.length} path edits; ${payload.merges.length} merges. Nothing is saved until confirmation.</span>` : "";
  document.getElementById("taxonomy-undo").disabled = !state.history.length;
  document.getElementById("taxonomy-redo").disabled = !state.future.length;
  document.getElementById("taxonomy-clear").disabled = total === 0;
  const reviewButton = document.getElementById("taxonomy-review");
  reviewButton.disabled = total === 0 || state.applying;
  reviewButton.textContent = state.applying ? "Applying…" : "Review and apply";
  document.getElementById("tree-status").textContent = total ? `${total} staged change${total === 1 ? "" : "s"}` : "All changes are staged locally until review.";
}
function observeDocumentLeaves() {
  state.documentObserver?.disconnect();
  if (typeof IntersectionObserver === "undefined") return;
  state.documentObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const path = parseDatasetPath(entry.target.dataset.autoDocumentPath);
      state.documentObserver.unobserve(entry.target);
      if (path) loadDocuments(path);
    }
  }, { rootMargin: "160px" });
  document.querySelectorAll("[data-auto-document-path]").forEach((node) => state.documentObserver.observe(node));
}
function renderEditor() {
  document.getElementById("tree-root").innerHTML = renderTreeNodes(buildDraftTree());
  renderChangeTray(); observeDocumentLeaves();
}

function renderDistribution(payload) {
  document.getElementById("distribution-root").innerHTML = (payload.distribution || []).map((item) => `<div class="distribution-row"><div class="distribution-head"><span>${escapeHtml(item.bucket || "-")}</span><span>${escapeHtml(item.usage_count || 0)} documents</span></div><div class="distribution-bar"><span style="width:${Math.max(0, Math.min(100, Number(item.share_pct) || 0))}%"></span></div></div>`).join("") || '<div class="workflow-footnote">No distribution data.</div>';
}

function validateLabel(label) {
  const value = String(label || "").trim().replace(/\s+/g, " ");
  if (!value || value.length > 180 || !/^[A-Za-z0-9][A-Za-z0-9 &'()/:,+.\-]*$/.test(value)) throw new Error("Use a non-empty English label (maximum 180 characters).");
  return value;
}
function rewriteExpanded(source, rewrite) {
  state.expanded = new Set([...state.expanded].map((key) => {
    const path = parseDatasetPath(key);
    return path && isPrefix(source, path) ? pathKey(rewrite([...path])) : key;
  }));
}
async function askLabel(message, value = "") {
  const answer = await window.ManzaraUI.prompt({ title: "Edit category", message, value, acceptLabel: "Stage change" });
  if (answer === null || answer === undefined || answer === false) return null;
  return validateLabel(typeof answer === "object" ? answer.value : answer);
}
function renameNode(path, rawLabel) {
  const label = validateLabel(rawLabel);
  state.editingPath = null;
  if (label === path.at(-1)) { renderEditor(); return; }
  stage(() => {
    for (const row of state.draft.values()) if (isPrefix(path, row.path)) row.path[path.length - 1] = label;
    rewriteExpanded(path, (expanded) => { expanded[path.length - 1] = label; return expanded; });
  });
}
async function addLevel(path, placement) {
  if (Math.max(...subtreeIds(path).map((id) => state.draft.get(id).path.length), 0) >= 8) return window.ManzaraUI.toast("A classification path can contain at most eight levels.");
  const label = await askLabel(`Name for the new level ${placement} “${path.at(-1)}”`);
  if (!label) return; const index = placement === "above" ? path.length - 1 : path.length;
  stage(() => {
    for (const row of state.draft.values()) if (isPrefix(path, row.path)) row.path.splice(index, 0, label);
    rewriteExpanded(path, (expanded) => { expanded.splice(index, 0, label); return expanded; });
  });
}
async function removeLevel(path) {
  if (path.length <= 2) return window.ManzaraUI.toast("A classification must keep at least two category levels.");
  if (terminalIds(path).length) return window.ManzaraUI.toast("This category has directly assigned classifications. Merge them first.");
  const confirmed = await window.ManzaraUI.confirm({ title: "Remove hierarchy level", message: `Promote every child of “${path.at(-1)}” one level?`, acceptLabel: "Stage removal", destructive: true });
  if (confirmed) stage(() => {
    for (const row of state.draft.values()) if (isPrefix(path, row.path)) row.path.splice(path.length - 1, 1);
    rewriteExpanded(path, (expanded) => { expanded.splice(path.length - 1, 1); return expanded; });
  });
}
function moveSubtree(source, target) {
  if (samePath(source, target) || isPrefix(source, target)) return window.ManzaraUI.toast("A category cannot be moved into itself or its descendants.");
  const ids = subtreeIds(source); if (!ids.length) return;
  if (ids.some((id) => target.length + 1 + state.draft.get(id).path.length - source.length > 8)) return window.ManzaraUI.toast("That move would exceed the eight-level limit.");
  stage(() => {
    for (const id of ids) { const row = state.draft.get(id); row.path = [...target, source.at(-1), ...row.path.slice(source.length)]; }
    rewriteExpanded(source, (expanded) => [...target, source.at(-1), ...expanded.slice(source.length)]);
  });
}
function mergeClassification(source, target) {
  if (source === target || state.merges.has(source) || state.merges.has(target) || [...state.merges.values()].includes(source)) return window.ManzaraUI.toast("That classification already participates in a staged merge.");
  if (!state.draft.has(source) || !state.draft.has(target)) return;
  state.mergeSource = null;
  stage(() => state.merges.set(source, target));
}

async function loadDocuments(path) {
  const page = documentState(path); if (page.loading) return;
  page.loading = true; page.error = ""; renderEditor();
  try {
    const payload = await api(`/api/library/classifications/documents?${new URLSearchParams({ classification_ids: documentGroup(path).join(","), offset: String(page.items.length), limit: "10" })}`);
    page.items.push(...(payload.items || [])); page.total = payload.total || page.items.length; page.hasMore = Boolean(payload.has_more);
  } catch (error) { page.error = String(error?.message || error); }
  finally { page.loading = false; renderEditor(); }
}
async function openDocument(md5) {
  const existing = state.documentDownloads.get(md5);
  if (existing?.openUrl) { window.open(existing.openUrl, "_blank", "noopener"); return; }
  const pendingWindow = window.open("about:blank", "_blank");
  if (pendingWindow) pendingWindow.opener = null;
  state.documentDownloads.set(md5, { loading: true, error: "" }); renderEditor();
  try {
    const payload = await api(`/api/library/documents/${encodeURIComponent(md5)}/cache`, { method: "POST" });
    const openUrl = String(payload?.open_url || "").trim();
    if (!openUrl) throw new Error("Local document URL was not returned");
    state.documentDownloads.set(md5, { loading: false, error: "", openUrl });
    if (pendingWindow) pendingWindow.location.href = openUrl;
    else window.open(openUrl, "_blank", "noopener");
  } catch (error) {
    pendingWindow?.close();
    state.documentDownloads.set(md5, { loading: false, error: String(error?.message || error) });
  }
  renderEditor();
}

async function reviewAndApply() {
  if (state.applying || !changeCount()) return;
  state.applying = true; renderChangeTray();
  try {
  const draft = stagedPayload();
  const preview = await api("/api/library/classifications/change-set/preview", { method: "POST", body: JSON.stringify(draft) });
  const summary = preview.summary || {};
  const details = [
    ...draft.changes.map((item) => {
      const before = state.base.get(item.classification_id)?.path.join(" / ") || `#${item.classification_id}`;
      return `${before} → ${item.path.join(" / ")}`;
    }),
    ...draft.merges.map((item) => `${state.base.get(item.source_classification_id)?.ddc || `#${item.source_classification_id}`} → ${state.base.get(item.target_classification_id)?.ddc || `#${item.target_classification_id}`}`),
  ];
  const detailText = details.slice(0, 8).join("\n");
  const remainder = details.length > 8 ? `\n…and ${details.length - 8} more` : "";
  const confirmed = await window.ManzaraUI.confirm({ title: "Apply category changes", message: `${summary.path_changes || 0} path changes, ${summary.classification_merges || 0} merges, ${summary.affected_documents || 0} affected documents.\n\n${detailText}${remainder}\n\nApply atomically?`, acceptLabel: "Apply changes", destructive: Boolean(summary.classification_merges) });
  if (!confirmed) return;
  await api("/api/library/classifications/change-set/apply", { method: "POST", body: JSON.stringify({ ...draft, confirmed: true, change_set_hash: preview.change_set_hash }) });
  await refreshAll(true); window.ManzaraUI.toast("Category changes applied.");
  } finally {
    state.applying = false; renderChangeTray();
  }
}

async function refreshTable() { renderTable(await api(tableUrl())); }
async function refreshInsights(resetDraft = false) {
  const payload = await api("/api/library/classifications/insights"); state.insightsPayload = payload;
  if (!payload.available) throw new Error(payload.error || "Hierarchy unavailable");
  if (resetDraft || !state.base.size) initializeDraft(payload);
  renderEditor(); renderDistribution(payload);
}
async function refreshGlobal() {
  const payload = await api("/api/library"); state.globalPayload = payload;
  document.getElementById("global-status").textContent = window.ManzaraCore.formatGlobalStatus(payload.global?.active_tasks || 0);
  window.ManzaraCore.applyStopAllButton(document.getElementById("stop-all-btn"), payload.global?.stop_all_state);
}
async function refreshAll(resetDraft = false) {
  await Promise.all([refreshTable(), refreshInsights(resetDraft), refreshGlobal()]);
  viewState.set(state.base.size ? "ready" : "empty"); tabController.apply(); lucide.createIcons();
}
function queueRefresh(delayMs = 250) { if (!changeCount()) window.ManzaraCore.scheduleRefresh(state, () => refreshAll(false), delayMs); }
function undo() { if (state.history.length) { state.future.push(snapshot()); restore(state.history.pop()); } }
function redo() { if (state.future.length) { state.history.push(snapshot()); restore(state.future.pop()); } }
function clearChanges() {
  if (!changeCount()) return; state.history.push(snapshot());
  state.draft = new Map([...state.base].map(([id, row]) => [id, { ...row, path: [...row.path] }]));
  state.merges = new Map(); state.future = []; state.documentPages = new Map();
  state.expanded = expandedPaths(state.draft.values()); renderEditor();
}
function parseDatasetPath(value) {
  try { const parsed = JSON.parse(decodeURIComponent(value)); return Array.isArray(parsed) ? parsed : null; }
  catch {
    try { const parsed = JSON.parse(value); return Array.isArray(parsed) ? parsed : null; }
    catch { return null; }
  }
}

function attachTreeHandlers() {
  const root = document.getElementById("tree-root");
  root.addEventListener("click", (event) => {
    const toggle = event.target.closest(".tree-toggle");
    if (toggle) { const path = parseDatasetPath(toggle.dataset.treePath); if (!path) return; const key = pathKey(path); state.expanded.has(key) ? state.expanded.delete(key) : state.expanded.add(key); renderEditor(); return; }
    const action = event.target.closest("[data-action]");
    if (action) {
      const path = parseDatasetPath(action.dataset.path); if (!path) return;
      const jobs = {
        "edit-name": () => { state.editingPath = pathKey(path); renderEditor(); },
        "save-name": () => renameNode(path, action.closest(".taxonomy-inline-editor")?.querySelector(".taxonomy-name-input")?.value),
        "cancel-name": () => { state.editingPath = null; renderEditor(); },
        "add-above": () => addLevel(path, "above"), "add-below": () => addLevel(path, "below"), remove: () => removeLevel(path),
      };
      Promise.resolve(jobs[action.dataset.action]?.()).catch((error) => window.ManzaraUI.toast(error.message)); return;
    }
    const load = event.target.closest(".document-load-btn, .document-more-btn");
    if (load) { const path = parseDatasetPath(load.dataset.path); if (path) loadDocuments(path); return; }
    const open = event.target.closest(".document-open-btn"); if (open) openDocument(open.dataset.md5);
    const pill = event.target.closest("[data-drop-classification]");
    if (pill) {
      const id = toInt(pill.dataset.dropClassification);
      if (state.mergeSource && state.mergeSource !== id) mergeClassification(state.mergeSource, id);
      else { state.mergeSource = state.mergeSource === id ? null : id; renderEditor(); }
    }
  });
  root.addEventListener("keydown", (event) => {
    if (!event.target.closest(".taxonomy-name-input")) return;
    if (event.key === "Enter") event.target.closest(".taxonomy-inline-editor")?.querySelector('[data-action="save-name"]')?.click();
    if (event.key === "Escape") event.target.closest(".taxonomy-inline-editor")?.querySelector('[data-action="cancel-name"]')?.click();
  });
  root.addEventListener("dragstart", (event) => {
    const pill = event.target.closest("[data-drag-classification]"); const row = event.target.closest("[data-drag-path]");
    if (pill) state.dragged = { type: "classification", id: toInt(pill.dataset.dragClassification) };
    else if (row) state.dragged = { type: "path", path: parseDatasetPath(row.dataset.dragPath) };
  });
  root.addEventListener("dragover", (event) => { if (event.target.closest("[data-drop-path], [data-drop-classification]")) event.preventDefault(); });
  root.addEventListener("drop", (event) => {
    event.preventDefault(); const targetClass = event.target.closest("[data-drop-classification]"); const targetPath = event.target.closest("[data-drop-path]");
    if (state.dragged?.type === "classification" && targetClass) mergeClassification(state.dragged.id, toInt(targetClass.dataset.dropClassification));
    else if (state.dragged?.type === "path" && targetPath) { const path = parseDatasetPath(targetPath.dataset.dropPath); if (path) moveSubtree(state.dragged.path, path); }
    state.dragged = null;
  });
  root.addEventListener("dragend", () => { state.dragged = null; });
}

function attachUiHandlers() {
  document.getElementById("filter-apply").addEventListener("click", () => { state.page = 1; state.activeTab = "table"; queueRefresh(0); });
  document.getElementById("page-prev").addEventListener("click", () => { if (state.page > 1) { state.page -= 1; queueRefresh(0); } });
  document.getElementById("page-next").addEventListener("click", () => { if (state.page < (state.tablePayload?.total_pages || 1)) { state.page += 1; queueRefresh(0); } });
  document.querySelector(".classification-tabs").addEventListener("click", (event) => { const button = event.target.closest(".classification-tab"); if (button) { state.activeTab = button.dataset.tab; tabController.apply(); } });
  document.getElementById("taxonomy-undo").addEventListener("click", undo);
  document.getElementById("taxonomy-redo").addEventListener("click", redo);
  document.getElementById("taxonomy-clear").addEventListener("click", clearChanges);
  document.getElementById("taxonomy-review").addEventListener("click", () => reviewAndApply().catch((error) => window.ManzaraUI.toast(error.message)));
  document.getElementById("stop-all-btn").addEventListener("click", async () => { await api("/api/system/stop-all", { method: "POST" }); queueRefresh(0); });
  attachTreeHandlers();
}

function renderLoading() {
  viewState.set("loading");
  document.getElementById("classification-table-status").textContent = "Loading classifications…";
  document.getElementById("tree-root").innerHTML = '<div class="workflow-footnote">Loading hierarchy…</div>';
  document.getElementById("distribution-root").innerHTML = '<div class="workflow-footnote">Loading distribution…</div>';
}
function renderError(error) {
  viewState.set("error");
  const message = String(error?.message || error || "Failed to load classifications.");
  document.getElementById("classification-table-status").textContent = `Classifications unavailable: ${message}`;
  document.getElementById("classification-table-status").classList.add("library-status-error");
  document.getElementById("tree-root").innerHTML = `<div class="workflow-footnote library-status-error">${escapeHtml(message)}</div>`;
  document.getElementById("distribution-root").innerHTML = `<div class="workflow-footnote library-status-error">${escapeHtml(message)}</div>`;
}
function setupEventStream() {
  state.eventStreamController = window.ManzaraCore.createSseController({
    eventTypes: window.ManzaraCore.DEFAULT_EVENT_TYPES,
    initialCursor: window.ManzaraCore.eventCursorFromSnapshot(state.globalPayload),
    getCursor: () => state.eventCursor, setCursor: (value) => { state.eventCursor = Number(value || 0); },
    onEvent: (payload) => { document.getElementById("last-event").textContent = window.ManzaraCore.formatEventBanner(payload); if (String(payload?.type || "").startsWith("library.")) queueRefresh(150); },
  });
  state.eventStreamController.start();
}
async function bootstrap() {
  window.addEventListener("beforeunload", (event) => {
    if (!changeCount()) return;
    event.preventDefault(); event.returnValue = "";
  });
  attachUiHandlers(); renderLoading(); await refreshAll(true); setupEventStream();
}
bootstrap().catch((error) => { console.error(error); renderError(error); });
