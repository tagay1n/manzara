"use strict";

const state = {snapshot:null, selected:new Set(), renames:new Map(), corrections:new Map(), merges:[], filter:"", sort:{field:"name",direction:"asc"}, aliases:new Set(), documents:new Map(), openDocuments:new Set(), loading:true, applying:false};
const api = (path, options={}) => window.ManzaraCore.api(path, options);
const esc = (value) => window.ManzaraCore.escapeHtml(value);
const key = (row) => String(row.key);
const fields = ["surname_full", "surname_initial", "name_full", "name_initial", "father_name_full", "father_name_initial", "title", "sex"];
const formIds = {surname_full:"personality-surname", surname_initial:"personality-surname-initial", name_full:"personality-name", name_initial:"personality-name-initial", father_name_full:"personality-father-name", father_name_initial:"personality-father-name-initial", title:"personality-title", sex:"personality-sex"};

function canonicalName(c) {
  const parts = [c.surname_full || c.surname_initial, c.name_full || c.name_initial, c.father_name_full || c.father_name_initial].filter(Boolean);
  if (c.father_name_full && c.sex === "M") parts.push("улы");
  if (c.father_name_full && c.sex === "F") parts.push("кызы");
  if (c.title) parts.push(c.title);
  return parts.join(" ");
}
function rows() {
  const merged = new Set();
  const draftMerges = state.merges.map((merge) => {
    const members = (state.snapshot?.items || []).filter((row) => merge.canonical_ids.includes(row.canonical_id));
    members.forEach((row) => merged.add(key(row)));
    return {key:`pending:${merge.canonical_ids.join("-")}`, display_name:merge.display_name, aliases:members.flatMap((row) => [row.display_name, ...row.aliases]), document_count:members.reduce((sum, row) => sum + Number(row.document_count || 0), 0), pending:true};
  });
  return (state.snapshot?.items || []).filter((row) => !merged.has(key(row))).map((row) => {
    const correction = state.corrections.get(key(row));
    if (correction) return {...row, display_name:canonicalName(correction), pending:true};
    if (state.renames.has(key(row))) return {...row, display_name:state.renames.get(key(row)), aliases:[...new Set([...row.aliases, row.display_name, state.renames.get(key(row))])], pending:true};
    return row;
  }).concat(draftMerges).filter((row) => !state.filter || [row.display_name, ...(row.aliases || [])].some((value) => String(value).toLocaleLowerCase().includes(state.filter.toLocaleLowerCase())))
    .sort((a, b) => {
      const order = state.sort.field === "documents"
        ? Number(a.document_count || 0) - Number(b.document_count || 0)
        : String(a.display_name || "").localeCompare(String(b.display_name || ""), undefined, {sensitivity:"base"});
      return state.sort.direction === "asc" ? order : -order;
    });
}
function changes() {
  const inMerge = new Set(state.merges.flatMap((item) => item.canonical_ids));
  return {snapshot_token:state.snapshot?.snapshot_token || "", renames:[...state.renames].filter(([value]) => !inMerge.has(Number(value.slice(10)))).map(([value, display_name]) => ({canonical_id:Number(value.slice(10)), display_name})), corrections:[...state.corrections].filter(([value]) => !inMerge.has(Number(value.slice(10)))).map(([value, components]) => ({canonical_id:Number(value.slice(10)), components})), merges:state.merges};
}
function pendingCount() { const payload = changes(); return payload.renames.length + payload.corrections.length + payload.merges.length; }
async function docs(row, page=1) { state.documents.set(key(row), {loading:true}); render(); try { state.documents.set(key(row), await api(`/api/library/personalities/documents?personality_key=${encodeURIComponent(key(row))}&page=${page}`)); } catch (error) { state.documents.set(key(row), {error:error.message || "Could not load documents."}); } render(); }
function render() {
  const visible = rows();
  document.getElementById("personality-sort-name").textContent = `Personality${state.sort.field === "name" ? (state.sort.direction === "asc" ? " ↑" : " ↓") : ""}`;
  document.getElementById("personality-sort-documents").textContent = `Documents${state.sort.field === "documents" ? (state.sort.direction === "asc" ? " ↑" : " ↓") : ""}`;
  document.getElementById("personality-count").textContent = state.loading ? "" : `${state.snapshot?.personality_count || 0} personalities`;
  const counts = state.snapshot?.decision_counts || {};
  document.getElementById("personality-decision-counts").textContent = `Not a person: ${counts.not_person || 0} · Needs review: ${counts.unusable || 0} · Failed: ${counts.failed || 0} · Deferred: ${counts.deferred || 0}`;
  document.getElementById("personality-clear").hidden = !state.filter;
  document.getElementById("personality-body").innerHTML = state.loading ? '<tr><td colspan="3" class="workflow-footnote">Loading personalities…</td></tr>' : visible.map((row) => {
    const id = key(row), expanded = state.aliases.has(id), aliases = (row.aliases || []).filter((value) => value !== row.display_name), open = state.openDocuments.has(id), page = state.documents.get(id);
    const links = !open ? "" : page?.loading ? "Loading documents…" : page?.error ? esc(page.error) : page ? `${page.items.map((item) => `<a href="/api/library/documents/${encodeURIComponent(item.md5)}/open" target="_blank" rel="noopener">${esc(item.label)}</a>`).join(" · ")}${page.has_more ? ` <button class="publisher-documents-next" data-key="${encodeURIComponent(id)}" data-page="${Number(page.page)+1}">Next 10</button>` : ""}` : "";
    return `<tr class="${row.pending ? "publisher-pending" : ""}"><td>${row.canonical_id ? `<input class="personality-select" type="checkbox" data-key="${encodeURIComponent(id)}" ${state.selected.has(id) ? "checked" : ""} aria-label="Select ${esc(row.display_name)}">` : ""}</td><td><div class="personality-name-row"><button class="publisher-name personality-name" data-key="${encodeURIComponent(id)}">${esc(row.display_name)}</button>${row.canonical_id ? `<button class="icon-btn quiet rename-inline-btn personality-correct" data-key="${encodeURIComponent(id)}" aria-label="Edit normalized details" title="Edit normalized details"><i data-lucide="square-pen" aria-hidden="true"></i></button>` : ""}${aliases.length ? `<button class="publisher-alias-toggle personality-alias-toggle personality-row-action" data-key="${encodeURIComponent(id)}" aria-expanded="${expanded}">${expanded ? "Hide" : "Show"} ${aliases.length} aliases</button>` : ""}<button class="publisher-documents-toggle personality-row-action" data-key="${encodeURIComponent(id)}" aria-expanded="${open}">${open ? "Hide" : "Show"} documents</button></div>${expanded && aliases.length ? `<div class="workflow-footnote publisher-aliases">${esc(aliases.join(" · "))}</div>` : ""}${open ? `<div class="workflow-footnote publisher-documents">${links}</div>` : ""}</td><td>${Number(row.document_count || 0)}</td></tr>`;
  }).join("") || '<tr><td colspan="3">No normalized personalities yet.</td></tr>';
  window.lucide?.createIcons?.();
  const selected = visible.filter((row) => row.canonical_id && state.selected.has(key(row)));
  document.getElementById("personality-merge").disabled = state.applying || selected.length < 2;
  document.getElementById("personality-apply").disabled = state.applying || !pendingCount();
  document.getElementById("personality-discard").disabled = state.applying || !pendingCount();
}
async function load() { state.loading=true; render(); try { state.snapshot=await api("/api/library/personalities"); document.getElementById("personality-status").textContent=""; } catch(error) { document.getElementById("personality-status").textContent=`Personalities unavailable: ${error.message || error}`; } state.loading=false; render(); }
function snapshotRow(id) { return (state.snapshot?.items || []).find((row) => key(row) === id); }

document.getElementById("personality-body").addEventListener("click", (event) => {
  const name=event.target.closest(".personality-name"), alias=event.target.closest(".personality-alias-toggle"), documentToggle=event.target.closest(".publisher-documents-toggle"), next=event.target.closest(".publisher-documents-next"), correct=event.target.closest(".personality-correct");
  if (name) { const row=snapshotRow(decodeURIComponent(name.dataset.key)); if (!row) return; const input=document.createElement("input"); input.className="filter-input"; input.value=row.display_name; name.replaceWith(input); input.focus(); const save=()=>{if(input.value.trim()) state.renames.set(key(row), input.value.trim()); render();}; input.addEventListener("keydown", (item)=>{if(item.key === "Enter")save();if(item.key === "Escape")render();}); input.addEventListener("blur",save); }
  if (correct) { const id=decodeURIComponent(correct.dataset.key), row=snapshotRow(id), components={...(row?.components || {}), ...(state.corrections.get(id) || {})}; fields.forEach((field) => { document.getElementById(formIds[field]).value=components[field] || ""; }); const dialog=document.getElementById("personality-correct-dialog"); dialog.dataset.key=id; dialog.showModal(); }
  if (alias) { const id=decodeURIComponent(alias.dataset.key); state.aliases.has(id) ? state.aliases.delete(id) : state.aliases.add(id); render(); }
  if (documentToggle) { const id=decodeURIComponent(documentToggle.dataset.key), row=snapshotRow(id); state.openDocuments.has(id) ? state.openDocuments.delete(id) : (state.openDocuments.add(id), docs(row)); render(); }
  if (next) { const id=decodeURIComponent(next.dataset.key); docs(snapshotRow(id), Number(next.dataset.page)); }
});
document.getElementById("personality-body").addEventListener("change", (event) => { const box=event.target.closest(".personality-select"); if (!box) return; const id=decodeURIComponent(box.dataset.key); box.checked ? state.selected.add(id) : state.selected.delete(id); render(); });
document.getElementById("personality-correct-form").addEventListener("submit", (event) => { event.preventDefault(); const dialog=document.getElementById("personality-correct-dialog"), components={}; fields.forEach((field)=>{components[field]=document.getElementById(formIds[field]).value || null;}); if (!components.surname_full && !components.surname_initial && !components.name_full && !components.name_initial) { window.ManzaraUI.toast("A surname or given name is required.", {tone:"warning"}); return; } state.corrections.set(dialog.dataset.key, components); state.renames.delete(dialog.dataset.key); dialog.close(); render(); });
document.getElementById("personality-filter").addEventListener("input", (event)=>{state.filter=event.target.value.trim();render();});
document.getElementById("personality-clear").addEventListener("click", ()=>{state.filter="";document.getElementById("personality-filter").value="";render();});
const toggleSort = (field) => { state.sort.direction = state.sort.field === field && state.sort.direction === "asc" ? "desc" : "asc"; state.sort.field = field; render(); };
document.getElementById("personality-sort-name").addEventListener("click", () => toggleSort("name"));
document.getElementById("personality-sort-documents").addEventListener("click", () => toggleSort("documents"));
document.getElementById("personality-all").addEventListener("change", (event)=>{rows().filter((row)=>row.canonical_id).forEach((row)=>event.target.checked?state.selected.add(key(row)):state.selected.delete(key(row)));render();});
document.getElementById("personality-merge").addEventListener("click", ()=>{const selected=rows().filter((row)=>row.canonical_id&&state.selected.has(key(row)));document.getElementById("personality-merge-name").value=selected[0].display_name;document.getElementById("personality-merge-dialog").showModal();});
document.getElementById("personality-merge-form").addEventListener("submit", (event)=>{event.preventDefault();const display_name=document.getElementById("personality-merge-name").value.trim(),canonical_ids=rows().filter((row)=>row.canonical_id&&state.selected.has(key(row))).map((row)=>row.canonical_id);if(display_name&&canonical_ids.length>1){state.merges.push({canonical_ids,display_name});state.selected.clear();document.getElementById("personality-merge-dialog").close();render();}});
document.getElementById("personality-discard").addEventListener("click", ()=>{state.renames.clear();state.corrections.clear();state.merges=[];state.selected.clear();render();});
document.getElementById("personality-apply").addEventListener("click", async()=>{state.applying=true;render();try{await api("/api/library/personalities/change-set/apply",{method:"POST",body:JSON.stringify(changes())});state.renames.clear();state.corrections.clear();state.merges=[];state.selected.clear();await load();}catch(error){window.ManzaraUI.toast(error.message||"Could not apply changes.",{tone:"error"});}finally{state.applying=false;render();}});
window.addEventListener("beforeunload", (event)=>{if(pendingCount()){event.preventDefault();event.returnValue="";}});
render();load();


const review = {page: 1, state: "all", payload: null, loading: true, error: "", request: 0, retrying: new Set()};
const decisionLabels = {not_person: "Not a person", unusable: "Needs review", failed: "Failed", deferred: "Deferred", retry_requested: "Retry requested"};
function renderDecisions() {
  const body = document.getElementById("personality-decisions-body");
  if (review.loading) body.innerHTML = '<tr><td colspan="4">Loading decisions…</td></tr>';
  else if (review.error) body.innerHTML = `<tr><td colspan="4">${esc(review.error)}</td></tr>`;
  else body.innerHTML = (review.payload?.items || []).map(row => {
    const canRetry = ["not_person", "unusable"].includes(row.state);
    return `<tr><td>${esc(row.raw_name)}</td><td><strong>${esc(decisionLabels[row.state] || row.state)}</strong><div class="workflow-footnote">${esc(row.reason || "")}</div></td><td>${Number(row.document_count || 0)}</td><td>${canRetry ? `<button class="small-btn personality-decision-retry" data-name="${encodeURIComponent(row.raw_name)}" data-updated-at="${encodeURIComponent(row.updated_at)}" ${review.retrying.has(row.raw_name) ? "disabled" : ""}>Retry after correction</button>` : ""}</td></tr>`;
  }).join("") || '<tr><td colspan="4">No decisions to review.</td></tr>';
  document.getElementById("personality-decisions-prev").disabled = review.loading || review.page <= 1;
  document.getElementById("personality-decisions-next").disabled = review.loading || !review.payload?.has_more;
  document.getElementById("personality-decisions-page").textContent = `Page ${review.page}`;
}
async function loadDecisions() {
  const request = ++review.request;
  review.loading = true; review.error = ""; renderDecisions();
  try {
    const payload = await api(`/api/library/personalities/decisions?state=${encodeURIComponent(review.state)}&page=${review.page}`);
    if (request === review.request) review.payload = payload;
  } catch (error) {
    if (request === review.request) { review.payload = null; review.error = `Review unavailable: ${error.message || error}`; }
  } finally {
    if (request === review.request) { review.loading = false; renderDecisions(); }
  }
}
document.getElementById("personality-decisions-state").addEventListener("change", event => {
  review.state = event.target.value; review.page = 1; loadDecisions();
});
document.getElementById("personality-decisions-prev").addEventListener("click", () => { if (review.page > 1) { review.page -= 1; loadDecisions(); } });
document.getElementById("personality-decisions-next").addEventListener("click", () => { if (review.payload?.has_more) { review.page += 1; loadDecisions(); } });
document.getElementById("personality-decisions-body").addEventListener("click", async event => {
  const button = event.target.closest(".personality-decision-retry");
  if (!button) return;
  const raw_name = decodeURIComponent(button.dataset.name), updated_at = decodeURIComponent(button.dataset.updatedAt);
  if (review.retrying.has(raw_name)) return;
  review.retrying.add(raw_name); renderDecisions();
  try {
    await api("/api/library/personalities/decisions/retry", {method: "POST", body: JSON.stringify({raw_name, updated_at})});
    await Promise.all([loadDecisions(), load()]);
    window.ManzaraUI.toast("Retry queued for the next normalization run.");
  } catch (error) { window.ManzaraUI.toast(error.message || "Could not request retry.", {tone: "error"}); }
  finally { review.retrying.delete(raw_name); renderDecisions(); }
});
renderDecisions(); loadDecisions();
