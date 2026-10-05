"use strict";

const state = {
  snapshot: null, review: null, tab: "directory", page: 1, detailKey: null,
  proposalId: null, clusterFilter: "cluster", reviewStatus: "pending", reviewDetailOpen: false,
  suggestionEdits: new Map(), detailEdits: new Map(), editTimer: null, editSave: null,
  selected: new Set(), renames: new Map(), keeps: new Set(), merges: [],
  documentsExpanded: new Set(), documents: new Map(), disclosures: new Map(),
  mergeTrigger: null, detailTrigger: null, filter: "", loading: true, error: "",
  applying: false, draftDirty: false, navigating: false, intentPending: false, busyLabel: "Applying changes…",
  sort: { field: "name", direction: "asc" },
};
const PAGE_SIZE = 50;
const api = (path, options = {}) => window.ManzaraCore.api(path, options);
const esc = (value) => window.ManzaraCore.escapeHtml(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const node = (id) => document.getElementById(id);
const rowKey = (row) => String(row.key);
const encoded = (value) => esc(encodeURIComponent(value));
const busy = () => state.applying || Boolean(state.editSave);

function projectedRows() {
  const base = (state.snapshot?.items || []).map(row => ({...row, aliases: [...(row.aliases || [])]}));
  const merged = new Set(state.merges.flatMap(merge => merge.members));
  const pending = state.merges.map(merge => {
    const members = base.filter(row => merge.members.includes(rowKey(row)));
    return {key: `pending:${merge.members.join("|")}`, canonical_id: null, display_name: merge.display_name,
      aliases: [...new Set(members.flatMap(row => [row.display_name, ...row.aliases]))].sort(),
      document_count: null, is_new: false, pending: true};
  });
  return base.filter(row => !merged.has(rowKey(row))).map(row => {
    const renamed = state.renames.get(rowKey(row));
    if (renamed) return {...row, display_name: renamed, pending: true, aliases: [...new Set([...row.aliases, row.display_name])]};
    return state.keeps.has(rowKey(row)) ? {...row, is_new: false, pending: true} : row;
  }).concat(pending);
}
function visibleRows() {
  const query = state.filter.toLocaleLowerCase();
  return projectedRows().filter(row => !query || [row.display_name, ...row.aliases].some(value => String(value).toLocaleLowerCase().includes(query)))
    .sort((a, b) => {
      if (a.is_new !== b.is_new) return Number(b.is_new) - Number(a.is_new);
      const value = state.sort.field === "documents" ? Number(a.document_count || 0) - Number(b.document_count || 0) : a.display_name.localeCompare(b.display_name);
      return (value || a.display_name.localeCompare(b.display_name)) * (state.sort.direction === "asc" ? 1 : -1);
    });
}
function pageRows() {
  const rows = visibleRows();
  state.page = Math.min(state.page, Math.max(1, Math.ceil(rows.length / PAGE_SIZE)));
  return rows.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE);
}
function selectedRows() { return projectedRows().filter(row => state.selected.has(rowKey(row)) && !row.pending); }
function changeSet() {
  const mergedIds = new Set(state.merges.flatMap(merge => merge.canonical_ids));
  const mergedNames = new Set(state.merges.flatMap(merge => merge.raw_names));
  return {snapshot_token: state.snapshot?.snapshot_token || "",
    renames: [...state.renames].filter(([key]) => key.startsWith("canonical:") && !mergedIds.has(Number(key.slice(10)))).map(([key, display_name]) => ({canonical_id: Number(key.slice(10)), display_name})),
    keeps: [...state.keeps].filter(key => !mergedNames.has(key.slice(4))).map(key => key.slice(4)),
    merges: state.merges.map(({canonical_ids, raw_names, display_name}) => ({canonical_ids, raw_names, display_name}))};
}
function pendingCount() { const changes = changeSet(); return changes.renames.length + changes.keeps.length + changes.merges.length; }
function hasReviewEdits() {
  return (state.review?.proposals || []).some(item => item.review_edit && item.status !== "staged") || [...state.suggestionEdits.values()].some(edit => edit.dirty);
}
function detailRow() { return projectedRows().find(row => row.key === state.detailKey); }

// Keep disclosure and input state when an asynchronous update replaces a pane.
function fragment(id, html) {
  const container = node(id);
  const focused = document.activeElement;
  const restoreFocus = focused?.id && container.contains?.(focused);
  const selection = restoreFocus ? [focused.selectionStart, focused.selectionEnd] : null;
  for (const detail of container.querySelectorAll?.("details[data-disclosure]") || []) {
    state.disclosures.set(detail.dataset.disclosure, detail.open);
  }
  container.innerHTML = html;
  for (const detail of container.querySelectorAll?.("details[data-disclosure]") || []) {
    if (state.disclosures.has(detail.dataset.disclosure)) detail.open = state.disclosures.get(detail.dataset.disclosure);
  }
  if (restoreFocus) {
    const replacement = node(focused.id);
    replacement?.focus?.();
    if (Number.isInteger(selection[0])) replacement?.setSelectionRange?.(...selection);
  }
}
function disclosure(key) { return `data-disclosure="${esc(key)}"`; }
function renderTabs() {
  for (const tab of ["directory", "review"]) {
    const active = state.tab === tab;
    node(`tab-btn-${tab}`).setAttribute("aria-selected", String(active));
    node(`tab-btn-${tab}`).tabIndex = active ? 0 : -1;
    node(`tab-btn-${tab}`).classList.toggle("active", active);
    node(`tab-panel-${tab}`).hidden = !active;
  }
  const count = (state.review?.proposals || []).filter(item => item.status === "pending").length;
  node("publisher-review-count").textContent = String(count);
  node("publisher-review-count").hidden = !count;
}
function renderChanges() {
  const pending = pendingCount();
  const edits = (state.review?.proposals || []).filter(item => item.status !== "staged" && (item.review_edit || state.suggestionEdits.get(item.proposal_id)?.dirty)).length;
  const detailDirty = [...state.detailEdits.values()].filter(edit => edit.dirty).length;
  node("publisher-changes").hidden = !pending && !edits && !detailDirty && !state.draftDirty;
  node("publisher-change-count").textContent = pending ? `${pending} staged${edits + detailDirty ? ` · ${edits + detailDirty} edited` : ""}` : `${edits + detailDirty} edited`;
  node("publisher-apply").disabled = busy() || pending === 0;
  node("publisher-discard").disabled = busy() || (!pending && !hasReviewEdits() && !detailDirty && !state.draftDirty);
  node("publisher-apply").innerHTML = state.applying ? `<span class="inline-spinner" aria-hidden="true"></span> ${esc(state.busyLabel)}` : "Apply changes";
  node("publisher-apply").setAttribute("aria-busy", String(state.applying));
}
function renderDirectory() {
  const all = visibleRows(), rows = pageRows();
  node("publisher-filter-clear").hidden = !state.filter;
  for (const field of ["name", "documents"]) {
    const button = node(`publisher-sort-${field}`);
    button.textContent = `${field === "name" ? "Publisher" : "Documents"}${state.sort.field === field ? (state.sort.direction === "asc" ? " ↑" : " ↓") : ""}`;
    button.parentElement?.setAttribute("aria-sort", state.sort.field === field ? (state.sort.direction === "asc" ? "ascending" : "descending") : "none");
  }
  fragment("publisher-table-body", state.loading ? '<tr><td colspan="4"><span class="inline-spinner" aria-hidden="true"></span> Loading publishers…</td></tr>' : state.error ? '<tr><td colspan="4">Could not load publishers.</td></tr>' : rows.map(row => {
    const key = rowKey(row);
    return `<tr class="${row.pending ? "publisher-pending" : ""} ${key === state.detailKey ? "publisher-active-row" : ""}">
      <td><input class="publisher-row-select" id="publisher-select-${encoded(key)}" type="checkbox" data-key="${encoded(key)}" ${state.selected.has(key) ? "checked" : ""} aria-label="Select ${esc(row.display_name)}" ${row.pending || busy() ? "disabled" : ""} /></td>
      <td><button id="publisher-open-${encoded(key)}" class="publisher-name" data-key="${encoded(key)}" aria-label="Open ${esc(row.display_name)}" aria-controls="publisher-detail" aria-expanded="${key === state.detailKey}">${esc(row.display_name)}</button></td>
      <td>${row.document_count === null ? "—" : Number(row.document_count || 0)}</td>
      <td>${row.is_new ? '<span class="panel-pill">New</span>' : row.pending ? '<span class="panel-pill">Pending</span>' : ""}</td></tr>`;
  }).join("") || `<tr><td colspan="4">${state.filter ? "No matches." : "No publishers yet."}</td></tr>`);
  const selected = selectedRows();
  node("publisher-selection").hidden = selected.length === 0;
  node("publisher-selected-count").textContent = `${selected.length} selected`;
  node("publisher-merge").disabled = busy() || selected.length < 2;
  const selectable = rows.filter(row => !row.pending);
  const checked = selectable.filter(row => state.selected.has(row.key)).length;
  node("publisher-select-page").checked = selectable.length > 0 && checked === selectable.length;
  node("publisher-select-page").indeterminate = checked > 0 && checked < selectable.length;
  node("publisher-select-page").disabled = busy() || !selectable.length;
  node("publisher-pager").hidden = state.loading || !all.length || Boolean(state.error);
  node("publisher-page-label").textContent = `${(state.page - 1) * PAGE_SIZE + 1}–${Math.min(state.page * PAGE_SIZE, all.length)} / ${all.length}`;
  node("publisher-page-previous").disabled = state.page === 1;
  node("publisher-page-next").disabled = state.page * PAGE_SIZE >= all.length;
  renderDetail();
}
function documentSection(key, context) {
  const expanded = state.documentsExpanded.has(key);
  const data = state.documents.get(key);
  const disabled = busy() ? "disabled" : "";
  const links = !expanded ? "" : data?.loading ? '<p class="workflow-footnote">Loading documents…</p>' : data?.error ? `<p class="workflow-footnote" role="status">${esc(data.error)}</p><button class="small-btn publisher-documents-next" data-key="${encoded(key)}" data-page="${Number(data.page || 1)}">Retry</button>` : data ? `<ul class="publisher-document-list">${data.items.map(item => `<li><a href="/api/library/documents/${encoded(item.md5)}/open" target="_blank" rel="noopener">${esc(item.label)}</a></li>`).join("") || '<li>No documents found.</li>'}</ul>${data.has_more ? `<button class="small-btn publisher-documents-next" data-key="${encoded(key)}" data-page="${Number(data.page || 1) + 1}" ${disabled}>Next 10</button>` : ""}` : "";
  return `<div class="publisher-document-section"><button class="publisher-documents-toggle" id="publisher-documents-${context}-${encoded(key)}" type="button" data-key="${encoded(key)}" aria-expanded="${expanded}" ${disabled}>Documents</button>${links}</div>`;
}
function renderDetail() {
  const row = detailRow();
  node("publisher-detail").hidden = !row;
  node("publisher-directory-layout").classList.toggle("has-detail", Boolean(row));
  if (!row) { fragment("publisher-detail-body", ""); return; }
  const aliases = row.aliases.filter(alias => alias !== row.display_name);
  const edit = state.detailEdits.get(row.key);
  const editable = row.canonical_id || row.key.startsWith("pending:");
  fragment("publisher-detail-body", `${editable ? `<label class="publisher-new-name">Name<input id="publisher-detail-name" class="filter-input" data-key="${encoded(row.key)}" maxlength="240" value="${esc(edit?.name ?? row.display_name)}" ${busy() ? "readonly" : ""} /></label><button class="small-btn publisher-detail-save" type="button" ${busy() || !edit?.dirty ? "disabled" : ""}>Stage rename</button>` : `<h3>${esc(row.display_name)}</h3>`}
    ${row.is_new ? `<button class="small-btn publisher-keep" type="button" data-key="${encoded(row.key)}" ${busy() ? "disabled" : ""}>Keep</button>` : ""}
    ${aliases.length ? `<details ${disclosure(`directory-aliases:${row.key}`)}><summary>Aliases (${aliases.length})</summary><ul>${aliases.map(alias => `<li>${esc(alias)}</li>`).join("")}</ul></details>` : ""}
    ${row.key.startsWith("pending:") ? "" : documentSection(row.key, "directory")}`);
}
function render() {
  renderTabs(); renderDirectory(); renderSuggestions(); renderChanges();
  const snapshot = state.snapshot || {};
  node("publisher-count").textContent = state.loading ? "" : `${snapshot.new_count ? `${snapshot.new_count} new · ` : ""}${snapshot.publisher_count || 0} publishers`;
  node("publisher-status").textContent = state.error;
  node("publisher-table-status").textContent = "";
}
function restoreReview(review) {
  state.review = review;
  const draft = review.draft || {};
  state.renames = new Map((draft.renames || []).map(item => [`canonical:${item.canonical_id}`, item.display_name]));
  state.keeps = new Set((draft.keeps || []).map(name => `raw:${name}`));
  state.merges = (draft.merges || []).map(item => ({...item, members: [...item.canonical_ids.map(id => `canonical:${id}`), ...item.raw_names.map(name => `raw:${name}`)]}));
}
async function load() {
  state.loading = true; state.error = ""; render();
  try {
    const [snapshot, review] = await Promise.all([api("/api/library/publishers"), api("/api/library/publishers/merge-suggestions")]);
    state.snapshot = snapshot; restoreReview(review);
    state.documents.clear(); state.documentsExpanded.clear();
  } catch (error) { state.error = `Publishers unavailable: ${error.message || error}`; }
  finally { state.loading = false; render(); }
}
function reviewedSnapshot() { return Object.fromEntries((state.snapshot?.items || []).map(row => [row.key, {display_name: row.display_name, aliases: [...(row.aliases || [])].sort(), is_new: row.is_new}])); }
async function persistDraft() {
  if (busy() || !state.review) return false;
  state.busyLabel = "Saving changes…"; state.applying = true; render();
  try {
    restoreReview(await api("/api/library/publishers/draft", {method: "PUT", body: JSON.stringify({revision: state.review.revision, changes: changeSet(), reviewed: reviewedSnapshot()})}));
    state.draftDirty = false; return true;
  } catch (error) { window.ManzaraUI.toast(error.message || "Could not save draft.", {tone: "error"}); return false; }
  finally { state.applying = false; render(); }
}
async function saveDetailEdit() {
  const row = detailRow(), edit = row && state.detailEdits.get(row.key);
  if (!edit?.dirty) return true;
  const name = edit.name.trim();
  if (!name || name.length > 240) { window.ManzaraUI.toast("Enter a publisher name (up to 240 characters).", {tone: "warning"}); return false; }
  const merge = state.merges.find(item => `pending:${item.members.join("|")}` === row.key);
  if (merge) merge.display_name = name;
  else if (row.canonical_id) {
    const original = state.snapshot.items.find(item => item.key === row.key);
    if (original.display_name === name) state.renames.delete(row.key); else state.renames.set(row.key, name);
  } else return false;
  state.draftDirty = true;
  const saved = await persistDraft();
  if (saved) { state.detailEdits.delete(row.key); render(); }
  return saved;
}
async function flushEdits() {
  clearTimeout(state.editTimer);
  if (state.applying) return false;
  if (!await persistSuggestionEdit()) return false;
  if (!await saveDetailEdit()) return false;
  return !state.draftDirty || await persistDraft();
}
async function runIntent(action) {
  if (busy() || state.intentPending || state.navigating) return;
  state.intentPending = true;
  try { await action(); }
  finally { state.intentPending = false; }
}
async function navigate(change, focusId) {
  if (state.navigating || state.intentPending) return;
  state.navigating = true;
  try { if (await flushEdits()) { change(); render(); if (focusId) node(focusId)?.focus?.(); } }
  finally { state.navigating = false; }
}
async function apply() {
  if (busy() || !pendingCount() || !await flushEdits()) return;
  state.busyLabel = "Applying changes…"; state.applying = true; render();
  try {
    await api("/api/library/publishers/change-set/apply", {method: "POST", body: JSON.stringify({use_draft: true, revision: state.review.revision})});
    state.selected.clear(); state.detailEdits.clear(); state.suggestionEdits.clear(); await load();
  } catch (error) { window.ManzaraUI.toast(error.message || "Could not apply publisher changes.", {tone: "error"}); }
  finally { state.applying = false; render(); }
}
async function discard() {
  if (busy()) return;
  clearTimeout(state.editTimer); state.applying = true; render();
  try {
    restoreReview(await api("/api/library/publishers/draft/discard", {method: "POST", body: JSON.stringify({revision: state.review.revision})}));
    state.selected.clear(); state.suggestionEdits.clear(); state.detailEdits.clear(); state.draftDirty = false;
  } catch (error) { window.ManzaraUI.toast(error.message || "Could not discard draft.", {tone: "error"}); }
  finally { state.applying = false; render(); }
}
function mergeChoices(query) {
  const needle = query.trim().toLocaleLowerCase();
  return (state.snapshot?.items || []).filter(row => row.canonical_id && [row.display_name, ...(row.aliases || [])].some(value => String(value).toLocaleLowerCase().includes(needle))).sort((a,b) => a.display_name.localeCompare(b.display_name));
}
function renderMergeChoices(query = "") {
  const matches = query.trim() ? mergeChoices(query) : [];
  node("publisher-merge-choices").hidden = !matches.length;
  node("publisher-merge-choices").innerHTML = matches.map(row => `<button class="publisher-merge-choice" type="button" data-name="${encoded(row.display_name)}">${esc(row.display_name)}</button>`).join("");
  node("publisher-merge-filter-clear").hidden = !query.trim();
}
function openMerge() {
  const picked = selectedRows();
  if (busy() || picked.length < 2) return;
  state.mergeTrigger = document.activeElement;
  node("publisher-merge-name").value = picked[0].display_name;
  node("publisher-merge-filter-input").value = "";
  node("publisher-merge-selected").innerHTML = picked.map(row => `<button class="publisher-merge-member" type="button" data-name="${encoded(row.display_name)}">${esc(row.display_name)}</button>`).join("");
  renderMergeChoices(); node("publisher-merge-dialog").showModal(); node("publisher-merge-name").focus?.();
}
async function confirmMerge(event) {
  event.preventDefault();
  const picked = selectedRows(), display_name = node("publisher-merge-name").value.trim();
  if (busy() || picked.length < 2 || !display_name || display_name.length > 240) return;
  if (!await flushEdits()) return;
  state.merges.push({members: picked.map(rowKey), canonical_ids: picked.map(row => row.canonical_id).filter(Number.isInteger), raw_names: picked.map(row => row.raw_name).filter(Boolean), display_name});
  picked.forEach(row => { state.selected.delete(row.key); state.renames.delete(row.key); state.keeps.delete(row.key); });
  state.draftDirty = true; node("publisher-merge-dialog").close(); await persistDraft();
}
function safeCitation(value) { return /^https?:\/\/[^\s/]+(?:[/?#]|$)/i.test(String(value)) && !/^https?:\/\/[^/]*@/i.test(String(value)) ? String(value) : null; }
function currentSuggestion() {
  const groups = (state.review?.proposals || []).filter(item => item.status === state.reviewStatus && (state.clusterFilter === "all" || item.proposal.kind === state.clusterFilter));
  const item = groups.find(item => item.proposal_id === state.proposalId) || groups[0];
  state.proposalId = item?.proposal_id ?? null;
  return {groups, item};
}
function suggestionEdit(item) {
  if (!state.suggestionEdits.has(item.proposal_id)) state.suggestionEdits.set(item.proposal_id, {members: [...(item.review_edit?.member_ids || item.proposal.member_ids)], name: item.review_edit?.display_name ?? item.proposal.proposed_name, dirty: false});
  return state.suggestionEdits.get(item.proposal_id);
}
function renderSuggestions() {
  const {groups, item} = currentSuggestion(), coverage = state.review?.coverage;
  node("publisher-cluster-filter").value = state.clusterFilter;
  node("publisher-review-status").value = state.reviewStatus;
  node("publisher-cluster-filter").disabled = busy(); node("publisher-review-status").disabled = busy();
  node("publisher-clustering-coverage").textContent = coverage?.inventory_entries ? `${coverage.covered_entries} / ${coverage.inventory_entries} entries · ${coverage.cluster_count} groups · ${coverage.singleton_entries} single · ${coverage.unresolved_entries} unresolved` : "No analysis yet.";
  node("publisher-suggestions-status").textContent = state.loading ? "Loading suggestions…" : state.error ? "Could not load suggestions." : groups.length ? "" : "No suggestions in this view.";
  fragment("publisher-suggestions-list", groups.map(entry => {
    const edit = suggestionEdit(entry);
    return `<button class="publisher-proposal-open ${entry.proposal_id === state.proposalId ? "is-selected" : ""}" id="publisher-proposal-${entry.proposal_id}" type="button" data-proposal-id="${entry.proposal_id}" aria-pressed="${entry.proposal_id === state.proposalId}" ${busy() ? "disabled" : ""}><span>${esc(edit.name)}</span><span class="publisher-proposal-meta">${edit.members.length}${entry.conflict_member_ids?.length ? ' · Conflict' : ''}${entry.proposal.confidence === "uncertain" ? ' · Uncertain' : ''}</span></button>`;
  }).join(""));
  node("publisher-review-detail").hidden = !item;
  node("publisher-review-layout").classList.toggle("has-detail", Boolean(item && state.reviewDetailOpen));
  if (!item) { fragment("publisher-suggestions-body", ""); return; }
  const group = item.proposal, edit = suggestionEdit(item), conflicts = item.conflict_member_ids || [];
  const members = item.members.filter(member => edit.members.includes(member.key));
  const unchangedCanonical = members.length === 1 && members[0].is_new === false && edit.name.trim() === members[0].display_name;
  const aliases = item.proposed_aliases || [];
  const citations = (group.citations || []).map(citation => { const url = safeCitation(citation.url); return url ? `<li><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(citation.supports)}</a></li>` : ""; }).join("");
  const conflict = conflicts.length ? `<div class="publisher-conflict" role="note"><strong>Conflict</strong><p>${members.filter(member => conflicts.includes(member.key)).map(member => esc(member.display_name)).join(" · ")}</p>${(item.conflicting_proposal_ids || []).map(id => {
    const other = (state.review?.proposals || []).find(entry => entry.proposal_id === id);
    return other?.status === "staged" ? `<p class="workflow-footnote">${esc(other.review_edit?.display_name || other.proposal.proposed_name)} is staged. Edit this group or discard the draft.</p>` : `<button class="publisher-conflict-open small-btn" type="button" data-proposal-id="${id}" ${busy() ? "disabled" : ""}>${esc(other?.review_edit?.display_name || other?.proposal.proposed_name || `Proposal ${id}`)}</button>`;
  }).join(" ")}</div>` : "";
  const actions = [["stage", edit.members.length === 1 ? (members[0]?.is_new === false ? "Stage rename" : "Keep") : "Stage merge"], ...(edit.members.length > 1 ? [["separate", "Keep separate"]] : []), ["skip", "Skip"]];
  fragment("publisher-suggestions-body", `<label class="publisher-new-name"><span class="sr-only">Name</span><input id="publisher-suggestion-name" class="filter-input publisher-proposed-name" aria-label="Canonical publisher name" maxlength="240" value="${esc(edit.name)}" ${state.applying ? "readonly" : ""} /></label>
    ${group.confidence === "uncertain" ? `<p class="workflow-footnote">Uncertain${group.uncertainty ? ` · ${esc(group.uncertainty)}` : ""}</p>` : ""}${conflict}
    <ul class="publisher-member-list">${members.map(member => `<li><div><span>${esc(member.display_name)}</span>${documentSection(member.key, "review")}</div><button class="publisher-suggestion-remove icon-btn" type="button" data-key="${encoded(member.key)}" aria-label="Remove ${esc(member.display_name)}" ${busy() || edit.members.length <= 1 ? "disabled" : ""}>×</button></li>`).join("")}</ul>
    ${aliases.length ? `<details ${disclosure(`review-aliases:${item.proposal_id}`)}><summary>Aliases (${aliases.length})</summary><ul>${aliases.map(alias => `<li>${esc(alias)}</li>`).join("")}</ul></details>` : ""}
    <details ${disclosure(`evidence:${item.proposal_id}`)}><summary>Evidence</summary><p>${esc(group.rationale)}</p>${group.confidence !== "uncertain" && group.uncertainty ? `<p>${esc(group.uncertainty)}</p>` : ""}${citations ? `<ul>${citations}</ul>` : ""}</details>
    <div class="publisher-review-actions">${actions.map(([action,label]) => `<button class="publisher-suggestion-action small-btn ${action === "stage" ? "publisher-primary" : ""}" type="button" data-action="${action}" ${busy() || (action === "stage" && (!edit.members.length || !edit.name.trim() || unchangedCanonical)) ? "disabled" : ""}${action === "separate" ? ' title="Saved immediately"' : ""}>${label}</button>`).join("")}</div>`);
}
async function persistSuggestionEdit() {
  if (state.editSave) return state.editSave;
  const {item} = currentSuggestion();
  if (!item || !suggestionEdit(item).dirty) return true;
  const edit = suggestionEdit(item);
  const save = async () => {
    while (edit.dirty) {
      if (!edit.name.trim() || !edit.members.length) {
        window.ManzaraUI.toast("Enter a name and retain at least one member.", {tone: "warning"}); return false;
      }
      const payload = {action: "edit", member_ids: [...edit.members], display_name: edit.name.trim()};
      try {
        const review = await api(`/api/library/publishers/merge-suggestions/${item.proposal_id}/review`, {method: "POST", body: JSON.stringify(payload)});
        restoreReview(review);
        if (JSON.stringify(edit.members) === JSON.stringify(payload.member_ids) && edit.name.trim() === payload.display_name) {
          edit.name = payload.display_name; edit.dirty = false;
        }
      } catch (error) { window.ManzaraUI.toast(error.message || "Could not save group edits.", {tone: "error"}); return false; }
    }
    return true;
  };
  state.editSave = save();
  renderChanges();
  try { return await state.editSave; }
  finally { state.editSave = null; render(); }
}
function scheduleSuggestionEdit() { clearTimeout(state.editTimer); state.editTimer = setTimeout(persistSuggestionEdit, 300); }
async function suggestionAction(action) {
  if (busy() || !await flushEdits()) return;
  const {groups,item} = currentSuggestion(); if (!item) return;
  const edit = suggestionEdit(item), index = groups.indexOf(item);
  state.busyLabel = "Saving review…"; state.applying = true; render();
  try {
    restoreReview(await api(`/api/library/publishers/merge-suggestions/${item.proposal_id}/review`, {method: "POST", body: JSON.stringify({action, member_ids: edit.members, display_name: edit.name.trim()})}));
    const remaining = currentSuggestion().groups.filter(entry => entry.proposal_id !== item.proposal_id);
    state.proposalId = remaining[Math.min(index, remaining.length - 1)]?.proposal_id ?? null;
    state.reviewDetailOpen = true;
  } catch (error) { window.ManzaraUI.toast(error.message || "Could not save review.", {tone: "error"}); }
  finally { state.applying = false; render(); if (currentSuggestion().item) node("publisher-suggestion-name")?.focus?.(); else node("publisher-cluster-filter").focus?.(); }
}
async function loadDocuments(key, page = 1) {
  const current = state.documents.get(key) || {items: []};
  state.documents.set(key, {...current, error: "", page, loading: true}); render();
  try { const payload = await api(`/api/library/publishers/documents?publisher_key=${encodeURIComponent(key)}&page=${page}`); state.documents.set(key, {...payload, loading: false}); }
  catch (error) { state.documents.set(key, {...current, page, loading: false, error: error.message || "Could not load documents."}); }
  render();
}
function documentClick(event) {
  const toggle = event.target.closest(".publisher-documents-toggle"), next = event.target.closest(".publisher-documents-next");
  if (toggle) {
    const key = decodeURIComponent(toggle.dataset.key);
    if (state.documentsExpanded.has(key)) {state.documentsExpanded.delete(key); render();}
    else {state.documentsExpanded.add(key); if (!state.documents.has(key)) loadDocuments(key); else render();}
  }
  if (next) loadDocuments(decodeURIComponent(next.dataset.key), Number(next.dataset.page));
}
function tabHandlers() {
  for (const tab of ["directory", "review"]) {
    node(`tab-btn-${tab}`).addEventListener("click", () => navigate(() => {state.tab = tab;}));
    node(`tab-btn-${tab}`).addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === "Home" ? "directory" : event.key === "End" ? "review" : tab === "directory" ? "review" : "directory";
      navigate(() => {state.tab = target;}, `tab-btn-${target}`);
    });
  }
}
function directoryHandlers() {
  node("publisher-table-body").addEventListener("change", event => {
    const box = event.target.closest(".publisher-row-select"); if (!box || busy()) return;
    const key = decodeURIComponent(box.dataset.key);
    box.checked ? state.selected.add(key) : state.selected.delete(key); render();
  });
  node("publisher-table-body").addEventListener("click", event => {
    const name = event.target.closest(".publisher-name");
    if (name) navigate(() => {state.detailKey = decodeURIComponent(name.dataset.key); state.detailTrigger = name.id;}, "publisher-detail-back");
  });
  node("publisher-detail-back").addEventListener("click", () => navigate(() => {state.detailKey = null;}, state.detailTrigger));
  node("publisher-detail-body").addEventListener("input", event => {
    if (event.target.id !== "publisher-detail-name" || busy()) return;
    const dirty = event.target.value.trim() !== detailRow()?.display_name;
    state.detailEdits.set(state.detailKey, {name: event.target.value, dirty});
    const save = node("publisher-detail-body").querySelector?.(".publisher-detail-save");
    if (save) save.disabled = !dirty;
    renderChanges();
  });
  node("publisher-detail-body").addEventListener("keydown", event => {
    if (event.target.id !== "publisher-detail-name") return;
    if (event.key === "Enter") {event.preventDefault(); runIntent(saveDetailEdit);}
    if (event.key === "Escape") {state.detailEdits.delete(state.detailKey); render();}
  });
  node("publisher-detail-body").addEventListener("click", async event => {
    if (busy()) return;
    documentClick(event);
    if (event.target.closest(".publisher-detail-save")) await runIntent(saveDetailEdit);
    const keep = event.target.closest(".publisher-keep");
    if (keep) await runIntent(async () => {
      if (!await flushEdits()) return;
      state.keeps.add(decodeURIComponent(keep.dataset.key)); state.draftDirty = true; await persistDraft();
    });
  });
  node("publisher-filter-input").addEventListener("input", event => {state.filter = event.target.value.trim(); state.page = 1; render();});
  node("publisher-filter-clear").addEventListener("click", () => {state.filter = ""; state.page = 1; node("publisher-filter-input").value = ""; render(); node("publisher-filter-input").focus?.();});
  node("publisher-select-page").addEventListener("change", event => {
    if (busy()) return;
    pageRows().filter(row => !row.pending).forEach(row => event.target.checked ? state.selected.add(row.key) : state.selected.delete(row.key)); render();
  });
  node("publisher-selection-clear").addEventListener("click", () => {state.selected.clear(); render();});
  for (const [id, delta] of [["publisher-page-previous", -1], ["publisher-page-next", 1]]) node(id).addEventListener("click", () => {state.page += delta; render();});
  for (const field of ["name", "documents"]) node(`publisher-sort-${field}`).addEventListener("click", () => {
    state.sort.direction = state.sort.field === field && state.sort.direction === "asc" ? "desc" : "asc";
    state.sort.field = field; state.page = 1; render();
  });
}
function reviewHandlers() {
  for (const [id,key] of [["publisher-cluster-filter", "clusterFilter"], ["publisher-review-status", "reviewStatus"]]) node(id).addEventListener("change", event => {
    const value = event.target.value;
    navigate(() => {state[key] = value; state.proposalId = null; state.reviewDetailOpen = false;});
  });
  const openProposal = id => navigate(() => {
    const target = (state.review?.proposals || []).find(item => item.proposal_id === id);
    if (!target || target.status === "staged") return;
    if (state.clusterFilter !== "all" && target.proposal.kind !== state.clusterFilter) state.clusterFilter = "all";
    state.reviewStatus = target.status; state.proposalId = id; state.reviewDetailOpen = true;
  }, "publisher-suggestion-name");
  node("publisher-suggestions-list").addEventListener("click", event => {
    const button = event.target.closest(".publisher-proposal-open"); if (button) openProposal(Number(button.dataset.proposalId));
  });
  node("publisher-review-back").addEventListener("click", () => navigate(() => {state.reviewDetailOpen = false;}, `publisher-proposal-${state.proposalId}`));
  node("publisher-suggestions-body").addEventListener("input", event => {
    if (event.target.id !== "publisher-suggestion-name" || state.applying) return;
    const {item} = currentSuggestion(); if (!item) return;
    const edit = suggestionEdit(item); edit.name = event.target.value; edit.dirty = true; renderChanges(); scheduleSuggestionEdit();
  });
  node("publisher-suggestions-body").addEventListener("click", event => {
    if (busy()) return;
    const related = event.target.closest(".publisher-conflict-open"), remove = event.target.closest(".publisher-suggestion-remove"), action = event.target.closest(".publisher-suggestion-action");
    if (related) openProposal(Number(related.dataset.proposalId));
    if (remove) {
      const {item} = currentSuggestion(), edit = suggestionEdit(item);
      if (edit.members.length <= 1) return;
      edit.members = edit.members.filter(key => key !== decodeURIComponent(remove.dataset.key)); edit.dirty = true;
      render(); clearTimeout(state.editTimer); persistSuggestionEdit();
    }
    if (action) runIntent(() => suggestionAction(action.dataset.action));
    documentClick(event);
  });
}
function mergeHandlers() {
  node("publisher-merge").addEventListener("click", openMerge);
  node("publisher-merge-form").addEventListener("submit", event => {event.preventDefault(); runIntent(() => confirmMerge(event));});
  node("publisher-merge-dialog").addEventListener("close", () => state.mergeTrigger?.focus?.());
  node("publisher-merge-filter-input").addEventListener("input", event => renderMergeChoices(event.target.value));
  node("publisher-merge-filter-clear").addEventListener("click", () => {node("publisher-merge-filter-input").value = ""; renderMergeChoices(); node("publisher-merge-filter-input").focus?.();});
  node("publisher-merge-dialog").addEventListener("click", event => {
    const choice = event.target.closest(".publisher-merge-choice, .publisher-merge-member"); if (choice) node("publisher-merge-name").value = decodeURIComponent(choice.dataset.name);
  });
}
tabHandlers(); directoryHandlers(); reviewHandlers(); mergeHandlers();
node("publisher-discard").addEventListener("click", () => runIntent(discard));
node("publisher-apply").addEventListener("click", () => runIntent(apply));
window.addEventListener("beforeunload", event => {
  if (!state.draftDirty && ![...state.detailEdits.values(), ...state.suggestionEdits.values()].some(edit => edit.dirty)) return;
  event.preventDefault(); event.returnValue = "";
});
render(); load();
