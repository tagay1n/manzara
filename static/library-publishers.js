"use strict";

const state = { snapshot: null, review: null, suggestionIndex: 0, clusterFilter: "cluster", suggestionEdits: new Map(), editTimer: null, selected: new Set(), renames: new Map(), keeps: new Set(), merges: [], editing: null, mergeTrigger: null, aliasesExpanded: new Set(), documentsExpanded: new Set(), documents: new Map(), filter: "", loading: true, applying: false, busyLabel: "Applying changes…", sort: { field: "name", direction: "asc" } };
const api = (path, options = {}) => window.ManzaraCore.api(path, options);
const esc = (value) => window.ManzaraCore.escapeHtml(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const rowKey = (row) => String(row.key);

function visibleRows() {
  const base = (state.snapshot?.items || []).map((row) => ({ ...row, aliases: [...(row.aliases || [])] }));
  const merged = new Set();
  const pending = state.merges.map((merge) => {
    const members = base.filter((row) => merge.members.includes(rowKey(row)));
    members.forEach((row) => merged.add(rowKey(row)));
    return { key: `pending:${merge.members.join("|")}`, canonical_id: merge.canonical_ids[0] || null, display_name: merge.display_name, aliases: [...new Set(members.flatMap((row) => [row.display_name, ...(row.aliases || [])]))].sort(), document_count: null, is_new: false, pending: true };
  });
  return base.filter((row) => !merged.has(rowKey(row))).map((row) => {
    const renamed = state.renames.get(rowKey(row));
    if (renamed) return { ...row, display_name: renamed, aliases: [...new Set([...(row.aliases || []), row.display_name, renamed])].sort() };
    return state.keeps.has(rowKey(row)) ? { ...row, is_new: false, pending: true, aliases: [row.display_name] } : row;
  }).concat(pending).filter((row) => {
    const query = state.filter.toLocaleLowerCase();
    return !query || [row.display_name, ...(row.aliases || [])].some((value) => String(value).toLocaleLowerCase().includes(query));
  }).sort((a, b) => {
    if (a.is_new !== b.is_new) return Number(b.is_new) - Number(a.is_new);
    const value = state.sort.field === "documents" ? Number(a.document_count || 0) - Number(b.document_count || 0) : a.display_name.localeCompare(b.display_name);
    const tie = a.display_name.localeCompare(b.display_name);
    return (value || tie) * (state.sort.direction === "asc" ? 1 : -1);
  });
}

function changeSet() {
  const mergedIds = new Set(state.merges.flatMap((merge) => merge.canonical_ids));
  const mergedNames = new Set(state.merges.flatMap((merge) => merge.raw_names));
  return { snapshot_token: state.snapshot?.snapshot_token || "", renames: [...state.renames].filter(([key]) => key.startsWith("canonical:") && !mergedIds.has(Number(key.slice(10)))).map(([key, display_name]) => ({ canonical_id: Number(key.slice(10)), display_name })), keeps: [...state.keeps].filter((key) => !mergedNames.has(key.slice(4))).map((key) => key.slice(4)), merges: state.merges.map(({ canonical_ids, raw_names, display_name }) => ({ canonical_ids, raw_names, display_name })) };
}
function pendingCount() { const changes = changeSet(); return changes.renames.length + changes.keeps.length + changes.merges.length; }
function documentsFor(key) { return state.documents.get(key); }
async function loadDocuments(key, page = 1) {
  const current = documentsFor(key) || { items: [] };
  state.documents.set(key, { ...current, loading: true }); render();
  try {
    const payload = await api(`/api/library/publishers/documents?publisher_key=${encodeURIComponent(key)}&page=${page}`);
    state.documents.set(key, { ...payload, loading: false });
  } catch (error) {
    state.documents.set(key, { ...current, loading: false, error: error.message || "Could not load documents." });
  }
  render();
}
function render() {
  renderSuggestions();
  const all = visibleRows(); const snapshot = state.snapshot || {};
  document.getElementById("publisher-count").textContent = state.loading ? "" : `${snapshot.new_count || 0} new · ${snapshot.publisher_count || 0} publishers`;
  document.getElementById("publisher-filter-clear").hidden = !state.filter;
  document.getElementById("publisher-sort-name").textContent = `Publisher${state.sort.field === "name" ? (state.sort.direction === "asc" ? " ↑" : " ↓") : ""}`;
  document.getElementById("publisher-sort-documents").textContent = `Documents${state.sort.field === "documents" ? (state.sort.direction === "asc" ? " ↑" : " ↓") : ""}`;
  document.getElementById("publisher-table-body").innerHTML = state.loading ? '<tr><td colspan="4" class="workflow-footnote"><span class="inline-spinner" aria-hidden="true"></span> Loading publishers…</td></tr>' : all.map((row) => {
    const key = rowKey(row); const editing = state.editing === key; const aliases = (row.aliases || []).filter((alias) => alias !== row.display_name);
    const expanded = state.aliasesExpanded.has(key); const documentsExpanded = state.documentsExpanded.has(key); const documents = documentsFor(key);
    const documentLinks = documentsExpanded ? (documents?.loading ? '<div class="workflow-footnote">Loading documents…</div>' : documents?.error ? `<div class="workflow-footnote">${esc(documents.error)}</div>` : documents ? `<div class="workflow-footnote publisher-documents">${documents.items.map((item) => `<a href="/api/library/documents/${encodeURIComponent(item.md5)}/open" target="_blank" rel="noopener">${esc(item.label)}</a>`).join(" · ") || "No documents found."}${documents.has_more ? `<button class="publisher-documents-next" data-key="${encodeURIComponent(key)}" data-page="${Number(documents.page || 1) + 1}">Next 10</button>` : ""}</div>` : "") : "";
    return `<tr class="${row.pending ? "publisher-pending" : ""}"><td><input class="publisher-row-select" type="checkbox" data-key="${encodeURIComponent(key)}" ${state.selected.has(key) ? "checked" : ""} aria-label="Select ${esc(row.display_name)}" ${row.key.startsWith("pending:") ? "disabled" : ""} /></td><td>${editing ? `<input class="publisher-name-input filter-input" data-key="${encodeURIComponent(key)}" value="${esc(row.display_name)}" aria-label="Publisher name" />` : `<button class="publisher-name" data-key="${encodeURIComponent(key)}" aria-label="Rename ${esc(row.display_name)}">${esc(row.display_name)}</button>`}${aliases.length ? `<div><button class="publisher-alias-toggle" data-key="${encodeURIComponent(key)}" aria-expanded="${expanded}">${expanded ? "Hide" : "Show"} ${aliases.length} alias${aliases.length === 1 ? "" : "es"}</button>${expanded ? `<div class="workflow-footnote publisher-aliases">${esc(aliases.join(" · "))}</div>` : ""}</div>` : ""}<div><button class="publisher-documents-toggle" data-key="${encodeURIComponent(key)}" aria-expanded="${documentsExpanded}">${documentsExpanded ? "Hide" : "Show"} documents</button>${documentLinks}</div></td><td>${row.document_count === null ? "—" : Number(row.document_count || 0)}</td><td>${row.is_new ? `<button class="small-btn publisher-keep" data-key="${encodeURIComponent(key)}">Keep</button>` : row.pending ? '<span class="panel-pill">Pending</span>' : ""}</td></tr>`;
  }).join("") || '<tr><td colspan="4">No publishers yet.</td></tr>';
  const selected = all.filter((row) => state.selected.has(rowKey(row)));
  const pending = pendingCount();
  const merge = document.getElementById("publisher-merge");
  const apply = document.getElementById("publisher-apply");
  const discard = document.getElementById("publisher-discard");
  merge.disabled = state.applying || selected.length < 2;
  apply.disabled = state.applying || pending === 0;
  discard.disabled = state.applying || (pending === 0 && !hasReviewEdits());
  apply.innerHTML = state.applying ? `<span class="inline-spinner" aria-hidden="true"></span> ${esc(state.busyLabel)}` : "Apply changes";
  apply.setAttribute("aria-busy", String(state.applying));
}
function stageRename(key, value) { const name = String(value || "").trim(); if (!name) { window.ManzaraUI.toast("Publisher name cannot be empty.", { tone: "warning" }); return; } const merge = state.merges.find(item => `pending:${item.members.join("|")}` === key); if (merge) {merge.display_name = name; state.editing = null; persistDraft(); return;} const row = (state.snapshot.items || []).find((item) => rowKey(item) === key); if (!row) return; if (row.is_new) state.keeps.add(key); state.renames.set(key, name); state.editing = null; render(); persistDraft(); }
function mergeChoices(query = "") { const needle = query.trim().toLocaleLowerCase(); return (state.snapshot?.items || []).filter((row) => row.canonical_id && (!needle || [row.display_name, ...(row.aliases || [])].some((value) => String(value).toLocaleLowerCase().includes(needle)))).sort((a, b) => a.display_name.localeCompare(b.display_name)); }
function renderMergeChoices(query = "") { const picker = document.getElementById("publisher-merge-choices"); const matches = query.trim() ? mergeChoices(query) : []; picker.hidden = matches.length === 0; picker.innerHTML = matches.map((row) => `<button class="publisher-merge-choice" type="button" role="option" data-name="${encodeURIComponent(row.display_name)}">${esc(row.display_name)}</button>`).join(""); document.getElementById("publisher-merge-filter-clear").hidden = !query.trim(); }
function openMerge() { const picked = visibleRows().filter((row) => state.selected.has(rowKey(row))); if (state.applying || picked.length < 2 || picked.some(row => row.key.startsWith("pending:"))) return; state.mergeTrigger = document.activeElement; document.getElementById("publisher-merge-name").value = picked[0]?.display_name || ""; document.getElementById("publisher-merge-filter-input").value = ""; document.getElementById("publisher-merge-selected").innerHTML = picked.map((row) => `<button class="publisher-merge-member" type="button" data-name="${encodeURIComponent(row.display_name)}">${esc(row.display_name)}</button>`).join(""); renderMergeChoices(); const dialog = document.getElementById("publisher-merge-dialog"); dialog.showModal(); document.getElementById("publisher-merge-name")?.focus?.(); }
function confirmMerge(event) { event.preventDefault(); const picked = visibleRows().filter((row) => state.selected.has(rowKey(row))); const display_name = document.getElementById("publisher-merge-name").value.trim(); if (!display_name) return; state.merges.push({ members: picked.map(rowKey), canonical_ids: picked.map((row) => row.canonical_id).filter(Number.isInteger), raw_names: picked.filter((row) => row.raw_name).map((row) => row.raw_name), display_name }); picked.forEach((row) => { state.selected.delete(rowKey(row)); state.renames.delete(rowKey(row)); state.keeps.delete(rowKey(row)); }); document.getElementById("publisher-merge-dialog").close(); render(); persistDraft(); }
function restoreReview(review) {
  state.review = review;
  const draft = review.draft || {};
  state.renames = new Map((draft.renames || []).map(item => [`canonical:${item.canonical_id}`, item.display_name]));
  state.keeps = new Set((draft.keeps || []).map(name => `raw:${name}`));
  state.merges = (draft.merges || []).map(item => ({...item, members: [...item.canonical_ids.map(id => `canonical:${id}`), ...item.raw_names.map(name => `raw:${name}`)]}));
}
async function load() {
  state.loading = true; render();
  const [snapshot, review] = await Promise.all([api("/api/library/publishers"), api("/api/library/publishers/merge-suggestions")]);
  state.snapshot = snapshot; restoreReview(review); state.loading = false;
  state.documents.clear(); state.documentsExpanded.clear();
  document.getElementById("publisher-status").textContent = ""; render();
}
function reviewedSnapshot() {
  return Object.fromEntries((state.snapshot?.items || []).map(row => [row.key, {display_name: row.display_name, aliases: [...row.aliases].sort(), is_new: row.is_new}]));
}
async function persistDraft() {
  if (state.applying || !state.review) return;
  state.busyLabel = "Saving changes…"; state.applying = true; render();
  try { restoreReview(await api("/api/library/publishers/draft", {method: "PUT", body: JSON.stringify({revision: state.review.revision, changes: changeSet(), reviewed: reviewedSnapshot()})})); }
  catch (error) { window.ManzaraUI.toast(error.message || "Could not save draft.", {tone: "error"}); await load(); }
  finally { state.applying = false; render(); }
}
async function apply() {
  if (state.applying || pendingCount() === 0) return;
  state.busyLabel = "Applying changes…"; state.applying = true; render();
  try { await api("/api/library/publishers/change-set/apply", {method: "POST", body: JSON.stringify({use_draft: true, revision: state.review.revision})}); state.selected.clear(); await load(); }
  catch (error) { window.ManzaraUI.toast(error.message || "Could not apply publisher changes.", {tone: "error"}); }
  finally { state.applying = false; render(); }
}
function hasReviewEdits() {return (state.review?.proposals || []).some(item => item.review_edit) || [...state.suggestionEdits.values()].some(edit => edit.dirty);}
async function discard() {
  if (state.applying || (pendingCount() === 0 && !hasReviewEdits())) return;
  clearTimeout(state.editTimer);
  state.applying = true; render();
  try { restoreReview(await api("/api/library/publishers/draft/discard", {method: "POST", body: JSON.stringify({revision: state.review.revision})})); state.selected.clear(); state.suggestionEdits.clear(); }
  catch (error) { window.ManzaraUI.toast(error.message || "Could not discard draft.", {tone: "error"}); }
  finally { state.applying = false; render(); }
}
function safeCitation(value) {
  return /^https?:\/\/[^\s/]+(?:[/?#]|$)/i.test(String(value)) && !/^https?:\/\/[^/]*@/i.test(String(value)) ? String(value) : null;
}
function currentSuggestion() {
  const groups = (state.review?.proposals || []).filter(item => item.status !== "staged" && (state.clusterFilter === "all" || item.proposal.kind === state.clusterFilter));
  state.suggestionIndex = Math.min(state.suggestionIndex, Math.max(0, groups.length - 1));
  return {groups, item: groups[state.suggestionIndex]};
}
function suggestionEdit(item) {
  if (!state.suggestionEdits.has(item.proposal_id)) state.suggestionEdits.set(item.proposal_id, {members: [...(item.review_edit?.member_ids || item.proposal.member_ids)], name: item.review_edit?.display_name || item.proposal.proposed_name, dirty: false});
  return state.suggestionEdits.get(item.proposal_id);
}
function renderSuggestions() {
  const {groups, item} = currentSuggestion();
  const coverage = state.review?.coverage;
  document.getElementById("publisher-cluster-filter").disabled = state.applying;
  document.getElementById("publisher-clustering-coverage").textContent = coverage?.inventory_entries ? `${coverage.covered_entries} of ${coverage.inventory_entries} inventory entries accounted for · ${coverage.cluster_count} clusters · ${coverage.singleton_entries} singleton publishers · ${coverage.unresolved_entries} unresolved entries` : "No clustering analysis yet. Run publisher clustering to account for the complete inventory.";
  document.getElementById("publisher-suggestions-status").textContent = state.loading ? "Loading suggestions…" : item ? `${state.suggestionIndex + 1} of ${groups.length}${item.status === "skipped" ? " · Skipped" : ""}` : "No entries awaiting review in this category.";
  document.getElementById("publisher-suggestions-previous").disabled = state.applying || state.suggestionIndex === 0;
  document.getElementById("publisher-suggestions-next").disabled = state.applying || state.suggestionIndex >= groups.length - 1;
  const body = document.getElementById("publisher-suggestions-body");
  if (!item) { body.innerHTML = ""; return; }
  const group = item.proposal; const edit = suggestionEdit(item);
  const citations = (group.citations || []).map(citation => { const url = safeCitation(citation.url); return url ? `<li><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(citation.supports)}</a></li>` : ""; }).join("");
  const conflicts = item.conflict_member_ids || [];
  const conflictNotice = conflicts.length ? `<div role="note"><span class="panel-pill">Conflict requires review</span><p>These publishers also appear in another proposal: ${item.members.filter(member => conflicts.includes(member.key)).map(member => esc(member.display_name)).join(" · ")}. Review the competing groups and choose which members belong together.</p>${(item.conflicting_proposal_ids || []).map(id => {
    const other = (state.review?.proposals || []).find(candidate => candidate.proposal_id === id);
    return other?.status === "staged" ? `<span class="workflow-footnote">Proposal #${Number(id)} is staged. Edit this group or discard the draft to reconsider it.</span>` : `<button class="publisher-conflict-open small-btn" type="button" data-proposal-id="${Number(id)}" ${state.applying ? "disabled" : ""}>Review proposal #${Number(id)}</button>`;
  }).join(" ")}</div>` : "";
  const kindLabel = {cluster: "Publisher cluster", singleton: "Singleton publisher", unresolved: "Unresolved entry"}[group.kind];
  const aliases = item.proposed_aliases || [];
  const selectedMembers = item.members.filter(member => edit.members.includes(member.key));
  const unchangedCanonical = selectedMembers.length === 1 && selectedMembers[0].is_new === false && edit.name === selectedMembers[0].display_name;
  body.innerHTML = `<p class="workflow-footnote">${esc(kindLabel)}</p><h3>Proposed publisher: ${esc(edit.name)}</h3><p>Proposed aliases (${aliases.length})</p>${aliases.length ? `<ul>${aliases.map(name => `<li>${esc(name)}</li>`).join("")}</ul>` : '<p class="workflow-footnote">No additional name variants in this entry.</p>'}${conflictNotice}${group.confidence === "uncertain" ? '<span class="panel-pill">Uncertain</span>' : ''}<p>${esc(group.rationale)}</p>${group.uncertainty ? `<p class="workflow-footnote">${esc(group.uncertainty)}</p>` : ""}<details><summary>Source entries (${selectedMembers.length})</summary><ul>${selectedMembers.map(member => `<li>${esc(member.display_name)} <span class="workflow-footnote">${esc(member.aliases.join(" · "))}</span> <button class="publisher-suggestion-remove small-btn" type="button" data-key="${encodeURIComponent(member.key)}" aria-label="Remove ${esc(member.display_name)}" ${state.applying ? "disabled" : ""}>Remove</button> <button class="publisher-documents-toggle small-btn" data-key="${encodeURIComponent(member.key)}" type="button">Show documents</button></li>`).join("")}</ul></details><label>Proposed publisher name <input id="publisher-suggestion-name" class="filter-input" aria-label="Canonical publisher name" maxlength="240" value="${esc(edit.name)}" ${state.applying ? "disabled" : ""}></label>${citations ? `<details><summary>Sources</summary><ul>${citations}</ul></details>` : ""}<div class="publisher-header-actions">${[["stage", edit.members.length === 1 ? "Keep publisher" : "Stage merge"], ...(edit.members.length > 1 ? [["separate", "Keep separate"]] : []), ["skip", "Skip"]].map(([action, label]) => `<button class="publisher-suggestion-action small-btn" type="button" data-action="${action}" ${state.applying || (action === "stage" && (!edit.members.length || unchangedCanonical)) ? "disabled" : ""}>${label}</button>`).join("")}</div>`;
}
async function suggestionAction(action) {
  const {item} = currentSuggestion(); if (!item || state.applying) return;
  clearTimeout(state.editTimer);
  const edit = suggestionEdit(item); state.busyLabel = "Saving review…"; state.applying = true; render();
  try { restoreReview(await api(`/api/library/publishers/merge-suggestions/${item.proposal_id}/review`, {method: "POST", body: JSON.stringify({action, member_ids: edit.members, display_name: edit.name})})); if (action === "skip" && item.status === "skipped") state.suggestionIndex = (state.suggestionIndex + 1) % Math.max(1, currentSuggestion().groups.length); }
  catch (error) { window.ManzaraUI.toast(error.message || "Could not save review.", {tone: "error"}); }
  finally {state.applying = false; render();}
}
async function persistSuggestionEdit() {
  const {item} = currentSuggestion(); if (!item || state.applying) return;
  const edit = suggestionEdit(item); if (!edit.dirty || !edit.name.trim() || !edit.members.length) return;
  const payload = {action: "edit", member_ids: [...edit.members], display_name: edit.name};
  try {
    const review = await api(`/api/library/publishers/merge-suggestions/${item.proposal_id}/review`, {method: "POST", body: JSON.stringify(payload)});
    item.review_edit = {member_ids: payload.member_ids, display_name: payload.display_name};
    let conflictsChanged = false;
    for (const update of review.proposals || []) {
      const existing = (state.review?.proposals || []).find(candidate => candidate.proposal_id === update.proposal_id);
      if (!existing) continue;
      const fields = {proposed_aliases: update.proposed_aliases || [], conflict_member_ids: update.conflict_member_ids || [], conflicting_proposal_ids: update.conflicting_proposal_ids || []};
      if (JSON.stringify(existing.proposed_aliases || []) !== JSON.stringify(fields.proposed_aliases) || JSON.stringify(existing.conflict_member_ids || []) !== JSON.stringify(fields.conflict_member_ids) || JSON.stringify(existing.conflicting_proposal_ids || []) !== JSON.stringify(fields.conflicting_proposal_ids)) conflictsChanged = true;
      Object.assign(existing, fields);
    }
    if (JSON.stringify(edit.members) === JSON.stringify(payload.member_ids) && edit.name === payload.display_name) edit.dirty = false;
    if (conflictsChanged && currentSuggestion().item?.proposal_id === item.proposal_id) {
      const focused = document.activeElement?.id === "publisher-suggestion-name";
      renderSuggestions();
      if (focused) document.getElementById("publisher-suggestion-name")?.focus?.();
    }
    document.getElementById("publisher-discard").disabled = state.applying;
  } catch (error) { if (!state.applying) window.ManzaraUI.toast(error.message || "Could not save group edits.", {tone: "error"}); }
}
function scheduleSuggestionEdit() {clearTimeout(state.editTimer); state.editTimer = setTimeout(persistSuggestionEdit, 300);}
function suggestionHandlers() {
  const body = document.getElementById("publisher-suggestions-body");
  document.getElementById("publisher-cluster-filter").addEventListener("change", async event => {
    clearTimeout(state.editTimer); await persistSuggestionEdit();
    state.clusterFilter = event.target.value; state.suggestionIndex = 0; render();
  });
  body.addEventListener("input", event => { if (event.target.id === "publisher-suggestion-name") {const {item} = currentSuggestion(); if (item) {suggestionEdit(item).name = event.target.value; suggestionEdit(item).dirty = true; scheduleSuggestionEdit();}} });
  body.addEventListener("click", async event => {
    const related = event.target.closest(".publisher-conflict-open");
    if (related && !state.applying) {
      clearTimeout(state.editTimer); await persistSuggestionEdit();
      const target = (state.review?.proposals || []).find(item => item.proposal_id === Number(related.dataset.proposalId));
      if (target && target.proposal.kind !== state.clusterFilter && state.clusterFilter !== "all") {
        state.clusterFilter = "all"; document.getElementById("publisher-cluster-filter").value = "all";
      }
      const index = currentSuggestion().groups.findIndex(item => item.proposal_id === Number(related.dataset.proposalId));
      if (index >= 0) {state.suggestionIndex = index; render();}
      return;
    }
    const remove = event.target.closest(".publisher-suggestion-remove");
    const action = event.target.closest(".publisher-suggestion-action");
    const documents = event.target.closest(".publisher-documents-toggle");
    if (remove && !state.applying) { const {item} = currentSuggestion(); const edit = suggestionEdit(item); edit.members = edit.members.filter(key => key !== decodeURIComponent(remove.dataset.key)); edit.dirty = true; render(); persistSuggestionEdit(); document.getElementById("publisher-suggestion-name")?.focus?.(); }
    if (action) suggestionAction(action.dataset.action);
    if (documents) { const key = decodeURIComponent(documents.dataset.key); loadSuggestionDocuments(key); }
  });
  document.getElementById("publisher-suggestions-previous").addEventListener("click", async () => {clearTimeout(state.editTimer); await persistSuggestionEdit(); state.suggestionIndex -= 1; render();});
  document.getElementById("publisher-suggestions-next").addEventListener("click", async () => {clearTimeout(state.editTimer); await persistSuggestionEdit(); state.suggestionIndex += 1; render();});
}
async function loadSuggestionDocuments(key) {
  try {
    const payload = await api(`/api/library/publishers/documents?publisher_key=${encodeURIComponent(key)}&page=1`);
    const body = document.getElementById("publisher-suggestions-body");
    const old = document.getElementById("publisher-suggestion-documents"); if (old) old.remove();
    const section = document.createElement("div"); section.id = "publisher-suggestion-documents";
    section.innerHTML = payload.items.map(item => `<a href="/api/library/documents/${encodeURIComponent(item.md5)}/open" target="_blank" rel="noopener">${esc(item.label)}</a>`).join(" · ") || "No documents found.";
    body.appendChild(section);
  } catch (error) {window.ManzaraUI.toast(error.message || "Could not load documents.", {tone: "error"});}
}

function handlers() { const body = document.getElementById("publisher-table-body"); body.addEventListener("change", (event) => { const box = event.target.closest(".publisher-row-select"); if (!box || state.applying) return; const key = decodeURIComponent(box.dataset.key); box.checked ? state.selected.add(key) : state.selected.delete(key); render(); }); body.addEventListener("click", (event) => { const keep = event.target.closest(".publisher-keep"); const name = event.target.closest(".publisher-name"); const aliases = event.target.closest(".publisher-alias-toggle"); const documents = event.target.closest(".publisher-documents-toggle"); const nextDocuments = event.target.closest(".publisher-documents-next"); if (keep) { state.keeps.add(decodeURIComponent(keep.dataset.key)); render(); persistDraft(); } if (aliases) { const key = decodeURIComponent(aliases.dataset.key); state.aliasesExpanded.has(key) ? state.aliasesExpanded.delete(key) : state.aliasesExpanded.add(key); render(); } if (documents) { const key = decodeURIComponent(documents.dataset.key); if (state.documentsExpanded.has(key)) { state.documentsExpanded.delete(key); render(); } else { state.documentsExpanded.add(key); if (!documentsFor(key)) loadDocuments(key); else render(); } } if (nextDocuments) loadDocuments(decodeURIComponent(nextDocuments.dataset.key), Number(nextDocuments.dataset.page)); if (name) { state.editing = decodeURIComponent(name.dataset.key); render(); document.querySelector(".publisher-name-input")?.focus(); } }); body.addEventListener("dblclick", (event) => { const name = event.target.closest(".publisher-name"); if (name) { state.editing = decodeURIComponent(name.dataset.key); render(); document.querySelector(".publisher-name-input")?.focus(); } }); body.addEventListener("keydown", (event) => { const name = event.target.closest(".publisher-name"); const input = event.target.closest(".publisher-name-input"); if (name && (event.key === "Enter" || event.key === "F2")) { event.preventDefault(); state.editing = decodeURIComponent(name.dataset.key); render(); document.querySelector(".publisher-name-input")?.focus(); } if (input && event.key === "Enter") { event.preventDefault(); stageRename(decodeURIComponent(input.dataset.key), input.value); } if (input && event.key === "Escape") { state.editing = null; render(); } }); body.addEventListener("focusout", (event) => { const input = event.target.closest(".publisher-name-input"); if (input) stageRename(decodeURIComponent(input.dataset.key), input.value); }); const filter = document.getElementById("publisher-filter-input"); filter.addEventListener("input", () => { state.filter = filter.value.trim(); render(); }); document.getElementById("publisher-filter-clear").addEventListener("click", () => { state.filter = ""; filter.value = ""; render(); filter.focus(); }); const mergeFilter = document.getElementById("publisher-merge-filter-input"); mergeFilter.addEventListener("input", () => renderMergeChoices(mergeFilter.value)); document.getElementById("publisher-merge-filter-clear").addEventListener("click", () => { mergeFilter.value = ""; renderMergeChoices(); mergeFilter.focus(); }); document.getElementById("publisher-merge-dialog").addEventListener("click", (event) => { const choice = event.target.closest(".publisher-merge-choice, .publisher-merge-member"); if (choice) document.getElementById("publisher-merge-name").value = decodeURIComponent(choice.dataset.name); }); const toggleSort = (field) => { state.sort.direction = state.sort.field === field && state.sort.direction === "asc" ? "desc" : "asc"; state.sort.field = field; render(); }; document.getElementById("publisher-sort-name").addEventListener("click", () => toggleSort("name")); document.getElementById("publisher-sort-documents").addEventListener("click", () => toggleSort("documents")); document.getElementById("publisher-select-page").addEventListener("change", (event) => { if (state.applying) return; visibleRows().filter(row => !row.key.startsWith("pending:")).forEach((row) => event.target.checked ? state.selected.add(rowKey(row)) : state.selected.delete(rowKey(row))); render(); }); document.getElementById("publisher-merge").addEventListener("click", openMerge); document.getElementById("publisher-merge-form").addEventListener("submit", confirmMerge); document.getElementById("publisher-merge-dialog").addEventListener("close", () => state.mergeTrigger?.focus?.()); document.getElementById("publisher-discard").addEventListener("click", discard); document.getElementById("publisher-apply").addEventListener("click", apply); }

handlers(); suggestionHandlers(); render(); load().catch((error) => { state.loading = false; render(); const message = `Publishers unavailable: ${error.message || error}`; document.getElementById("publisher-status").textContent = message; document.getElementById("publisher-table-status").textContent = message; });
