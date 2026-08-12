const state = {
  payload: null,
  viewState: "loading",
  taskId: null,
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
  selectedRunId: null,
  selectedShayanChangeType: "added",
  shayanChangesCache: {},
  logViewer: null,
  soundNotifier: null,
  conveyorDraft: null,
  conveyorSaving: false,
  conveyorDrag: null,
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

async function api(path, options = {}) {
  return window.ManzaraCore.api(path, options);
}

function escapeHtml(value) {
  return window.ManzaraCore.escapeHtml(value);
}

function cssName(name, fallback = "unknown") {
  return window.ManzaraCore.cssName(name, fallback);
}

function formatDateTime(value) {
  return window.ManzaraCore.formatDateTime(value);
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

function maybeShowTaskActionError(result) {
  window.ManzaraUI?.reportTaskActionResult(result);
}

function isActiveStatus(status) {
  return window.ManzaraCore.isActiveStatus(status);
}

function runDuration(run) {
  if (!run?.started_at) return "-";
  const start = Date.parse(run.started_at);
  if (Number.isNaN(start)) return "-";
  const endValue = run.finished_at ? Date.parse(run.finished_at) : Date.now();
  if (Number.isNaN(endValue)) return "-";
  const seconds = Math.max(0, Math.floor((endValue - start) / 1000));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}

function conveyorId(prefix) {
  if (globalThis.crypto?.randomUUID) return `${prefix}-${globalThis.crypto.randomUUID()}`;
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function conveyorTaskMap() {
  return new Map(
    (state.payload?.conveyor?.available_tasks || []).map((task) => [String(task.task_id), task])
  );
}

function conveyorRunActive() {
  return ["starting", "running"].includes(String(state.payload?.conveyor?.run?.status || ""));
}

function conveyorLockedItemIds() {
  if (!conveyorRunActive()) return new Set();
  return new Set(
    (state.payload?.conveyor?.items || [])
      .filter((item) => String(item.status || "pending") !== "pending")
      .map((item) => String(item.item_id))
  );
}

function syncConveyorDraft(payload) {
  const stages = payload?.conveyor?.definition?.stages;
  state.conveyorDraft = JSON.parse(JSON.stringify(Array.isArray(stages) ? stages : []));
}

function conveyorItemState(itemId) {
  return (state.payload?.conveyor?.items || []).find(
    (item) => String(item.item_id) === String(itemId)
  ) || null;
}

function conveyorStatusText() {
  const run = state.payload?.conveyor?.run;
  if (!run) return "Arrange rows top-to-bottom. Tasks in one row run in parallel.";
  const status = String(run.status || "idle");
  if (status === "completed" && run.outcome === "no_op") {
    return "Completed early: a sequential task produced no new work.";
  }
  if (status === "failed") return String(run.error_text || "Conveyor failed.");
  if (status === "stopped") return "Conveyor stopped; pending rows were canceled.";
  if (status === "running" || status === "starting") {
    if (run.stop_requested) return "Stopping after the current tasks reach a safe boundary.";
    return "Running. Completed and current rows are locked; future rows remain editable.";
  }
  return `Last conveyor run: ${status}.`;
}

function renderConveyorPalette(tasks) {
  if (!tasks.length) return '<div class="conveyor-empty">No tasks available.</div>';
  const groups = new Map();
  tasks.forEach((task) => {
    const key = String(task.panel_title || task.panel_id || "Other");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(task);
  });
  return [...groups.entries()].map(([title, items]) => `
    <section class="conveyor-palette-group">
      <h4>${escapeHtml(title)}</h4>
      ${items.map((task) => `
        <div class="conveyor-palette-task" draggable="true" data-conveyor-task-id="${escapeHtml(task.task_id)}">
          <span>${escapeHtml(task.title)}</span>
          <button class="conveyor-add-task small-btn quiet" type="button"
            data-task-id="${escapeHtml(task.task_id)}" aria-label="Add ${escapeHtml(task.title)} as a new row">+</button>
        </div>
      `).join("")}
    </section>
  `).join("");
}

function conveyorItemHtml(item, stageIndex, itemIndex, locked, taskMap) {
  const task = taskMap.get(String(item.task_id)) || { title: item.task_id, panel_title: "Unknown" };
  const runtime = conveyorItemState(item.item_id);
  const status = String(runtime?.status || "pending");
  const progress = runtime?.progress && typeof runtime.progress === "object" ? runtime.progress : {};
  const progressModel = window.ManzaraCore.taskStatusBadgeModel({ status, progress });
  const progressLabel = progressModel.determinate
    ? ` • ${progressModel.current} / ${progressModel.total} · ${progressModel.percent}%`
    : "";
  const meaningful = runtime?.meaningful;
  const resultLabel = meaningful === false ? " • no-op" : "";
  return `
    <article class="conveyor-item conveyor-status-${cssName(status)} ${locked ? "is-locked" : ""}"
      draggable="${locked ? "false" : "true"}" data-conveyor-item-id="${escapeHtml(item.item_id)}">
      <div class="conveyor-item-main">
        <strong>${escapeHtml(task.title)}</strong>
        <span>${escapeHtml(task.panel_title || task.panel_id || "")} • ${escapeHtml(status)}${resultLabel}${escapeHtml(progressLabel)}</span>
      </div>
      ${progressModel.determinate ? `<div class="conveyor-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progressModel.percent}" aria-label="${escapeHtml(task.title)}: ${progressModel.current} of ${progressModel.total}"><span style="width:${progressModel.percent}%"></span></div>` : ""}
      <div class="conveyor-item-actions">
        <button type="button" class="icon-btn quiet conveyor-item-prev" data-stage-index="${stageIndex}" data-item-index="${itemIndex}" ${locked || stageIndex === 0 ? "disabled" : ""} aria-label="Move to previous row" title="Move to previous row"><i data-lucide="arrow-up-to-line"></i></button>
        <button type="button" class="icon-btn quiet conveyor-item-next" data-stage-index="${stageIndex}" data-item-index="${itemIndex}" ${locked ? "disabled" : ""} aria-label="Move to next row" title="Move to next row"><i data-lucide="arrow-down-to-line"></i></button>
        <button type="button" class="icon-btn quiet conveyor-remove-item" data-stage-index="${stageIndex}" data-item-index="${itemIndex}" ${locked ? "disabled" : ""} aria-label="Remove task" title="Remove task"><i data-lucide="x"></i></button>
      </div>
    </article>
  `;
}

function renderConveyorStages(stages, taskMap, lockedIds) {
  if (!stages.length) {
    return '<div class="conveyor-new-row-drop" data-new-stage-index="0">Drop a task here to create the first row</div>';
  }
  const lockedStageIndexes = stages
    .map((stage, index) => stage.items.some((item) => lockedIds.has(String(item.item_id))) ? index : -1)
    .filter((index) => index >= 0);
  const minimumEditableIndex = lockedStageIndexes.length ? Math.max(...lockedStageIndexes) + 1 : 0;
  return stages.map((stage, stageIndex) => {
    const locked = stage.items.some((item) => lockedIds.has(String(item.item_id)));
    return `
      ${stageIndex >= minimumEditableIndex ? `<div class="conveyor-new-row-drop" data-new-stage-index="${stageIndex}">Drop for a sequential row</div>` : ""}
      <section class="conveyor-stage ${locked ? "is-locked" : ""}" data-stage-index="${stageIndex}">
        <div class="conveyor-stage-head">
          <span>Stage ${stageIndex + 1}${stage.items.length > 1 ? " • parallel" : ""}</span>
          <div>
            <button type="button" class="icon-btn quiet conveyor-stage-up" data-stage-index="${stageIndex}" ${locked || stageIndex <= minimumEditableIndex ? "disabled" : ""} aria-label="Move stage up"><i data-lucide="arrow-up"></i></button>
            <button type="button" class="icon-btn quiet conveyor-stage-down" data-stage-index="${stageIndex}" ${locked || stageIndex === stages.length - 1 ? "disabled" : ""} aria-label="Move stage down"><i data-lucide="arrow-down"></i></button>
          </div>
        </div>
        <div class="conveyor-stage-items" ${locked ? "" : `data-stage-drop-index="${stageIndex}"`}>
          ${stage.items.map((item, itemIndex) => conveyorItemHtml(item, stageIndex, itemIndex, locked, taskMap)).join("")}
        </div>
      </section>
    `;
  }).join("") + `<div class="conveyor-new-row-drop" data-new-stage-index="${stages.length}">Drop for a final sequential row</div>`;
}

function renderConveyor() {
  const root = document.getElementById("conveyor-stages");
  const palette = document.getElementById("conveyor-palette");
  if (!root || !palette) return;
  const conveyor = state.payload?.conveyor || {};
  if (!state.conveyorDraft) syncConveyorDraft(state.payload);
  const tasks = Array.isArray(conveyor.available_tasks) ? conveyor.available_tasks : [];
  const lockedIds = conveyorLockedItemIds();
  palette.innerHTML = renderConveyorPalette(tasks);
  root.innerHTML = renderConveyorStages(state.conveyorDraft || [], conveyorTaskMap(), lockedIds);
  const statusNode = document.getElementById("conveyor-status");
  if (statusNode) statusNode.textContent = conveyorStatusText();
  const active = conveyorRunActive();
  const runBtn = document.getElementById("conveyor-run");
  const stopBtn = document.getElementById("conveyor-stop");
  const clearBtn = document.getElementById("conveyor-clear");
  if (runBtn) {
    runBtn.hidden = active;
    runBtn.disabled = !state.conveyorDraft?.length || state.conveyorSaving;
  }
  if (stopBtn) stopBtn.hidden = !active;
  if (clearBtn) clearBtn.disabled = state.conveyorSaving || lockedIds.size > 0;
}

async function saveConveyorDraft() {
  if (state.conveyorSaving) return;
  state.conveyorSaving = true;
  renderConveyor();
  try {
    const revision = Number(state.payload?.conveyor?.definition?.revision || 0);
    const result = await api("/api/conveyor", {
      method: "PUT",
      body: JSON.stringify({ revision, stages: state.conveyorDraft || [] }),
    });
    state.payload.conveyor.definition = result.definition;
    syncConveyorDraft(state.payload);
  } catch (error) {
    window.ManzaraUI.toast(error?.message || String(error), { tone: "error" });
    await refreshTaskDetail();
  } finally {
    state.conveyorSaving = false;
    renderConveyor();
  }
}

function addTaskAsStage(taskId, stageIndex = null) {
  const task = conveyorTaskMap().get(String(taskId));
  if (!task) return;
  const stage = {
    stage_id: conveyorId("stage"),
    items: [{ item_id: conveyorId("item"), task_id: String(taskId) }],
  };
  const index = stageIndex === null ? state.conveyorDraft.length : Number(stageIndex);
  state.conveyorDraft.splice(index, 0, stage);
  saveConveyorDraft().catch((error) => console.error(error));
}

function removeConveyorItem(stageIndex, itemIndex) {
  const stage = state.conveyorDraft?.[stageIndex];
  if (!stage) return;
  stage.items.splice(itemIndex, 1);
  if (!stage.items.length) state.conveyorDraft.splice(stageIndex, 1);
}

function moveConveyorItem(stageIndex, itemIndex, targetStageIndex) {
  const source = state.conveyorDraft?.[stageIndex];
  const target = state.conveyorDraft?.[targetStageIndex];
  if (!source || !target) return;
  const [item] = source.items.splice(itemIndex, 1);
  if (!item || target.items.some((entry) => entry.task_id === item.task_id)) {
    if (item) source.items.splice(itemIndex, 0, item);
    return;
  }
  target.items.push(item);
  if (!source.items.length) state.conveyorDraft.splice(stageIndex, 1);
}

function selectedRun() {
  if (!state.payload?.runs?.length) return null;
  const selected = state.payload.runs.find((run) => run.run_id === state.selectedRunId);
  if (selected) return selected;
  state.selectedRunId = state.payload.runs[0].run_id;
  return state.payload.runs[0];
}

function runSummary(run) {
  const summary = run?.summary;
  return summary && typeof summary === "object" ? summary : {};
}

function runSummaryMessage(run) {
  const summary = runSummary(run);
  const message = String(summary.message || "").trim();
  if (message) return message;
  const status = String(run?.status || "idle");
  if (status === "completed") return "Run completed.";
  if (status === "failed") return String(run?.error_text || "Run failed.");
  if (status === "stopped") return "Run stopped.";
  return `Run ${status}.`;
}

function isShayanScanDetailsEnabled(run) {
  if (!run || !state.payload?.task) return false;
  if (String(state.payload.task.task_id || "") !== "shayan.scan_changes") return false;
  const summary = runSummary(run);
  const artifacts = summary?.artifacts;
  return artifacts && typeof artifacts === "object" && artifacts.kind === "shayan.snapshot_diff";
}

function shayanChangeCacheKey(runId, changeType) {
  return `${String(runId || 0)}:${String(changeType || "")}`;
}

function getShayanChangeCache(runId, changeType) {
  const key = shayanChangeCacheKey(runId, changeType);
  if (!state.shayanChangesCache[key]) {
    state.shayanChangesCache[key] = {
      items: [],
      nextAfterChangeId: 0,
      hasMore: false,
      loading: false,
      loaded: false,
      error: null,
      stats: null,
    };
  }
  return state.shayanChangesCache[key];
}

function formatEpisodeLabel(item) {
  const season = Number(item?.season || 0);
  const episode = Number(item?.episode || 0);
  if (season > 0 && episode > 0) {
    return `S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
  }
  if (episode > 0) return `E${String(episode).padStart(2, "0")}`;
  return "-";
}

function renderShayanChangesRows(items) {
  if (!Array.isArray(items) || !items.length) {
    return '<div class="run-row">No rows in this category.</div>';
  }
  return items
    .map((item) => {
      const program = String(item?.program || "-");
      const category = String(item?.category || "-");
      const title = String(item?.title || "-");
      const episodeLabel = formatEpisodeLabel(item);
      return `
        <div class="shayan-change-row">
          <div class="shayan-change-head">
            <span class="task-run-status">${escapeHtml(category)}</span>
            <span class="task-run-id">${escapeHtml(program)}</span>
            <span class="task-run-time">${escapeHtml(episodeLabel)}</span>
          </div>
          <div class="shayan-change-title">${escapeHtml(title)}</div>
          <div class="workflow-footnote">${escapeHtml(String(item?.entry_key || ""))}</div>
        </div>
      `;
    })
    .join("");
}

function renderShayanChangesSection(run) {
  if (!isShayanScanDetailsEnabled(run)) return "";
  const runId = Number(run?.run_id || 0);
  if (!runId) return "";
  const changeType = String(state.selectedShayanChangeType || "added");
  const cache = getShayanChangeCache(runId, changeType);
  if (!cache.loaded && !cache.error) return "";
  const stats = cache.stats || {};
  const counts = {
    added: Number(stats.added || 0),
    changed: Number(stats.changed || 0),
    removed: Number(stats.removed || 0),
  };
  const total = Number(stats.total || (counts.added + counts.changed + counts.removed));
  if (!cache.error && total <= 0) return "";
  const listBody = cache.error
    ? `<div class="run-row">Error: ${escapeHtml(cache.error)}</div>`
    : renderShayanChangesRows(cache.items);
  const loadMoreBtn = cache.hasMore
    ? `<button class="small-btn" data-load-shayan-more="1">Load more</button>`
    : "";

  return `
    <section class="shayan-change-section" data-shayan-run-id="${runId}">
      <div class="shayan-change-header">
        <div class="meta-k">Detailed changes</div>
        <div class="workflow-footnote">Total: ${escapeHtml(String(total))}</div>
      </div>
      <div class="shayan-change-tabs">
        <button class="small-btn ${changeType === "added" ? "active-tab" : ""}" data-shayan-change-type="added">Added (${counts.added})</button>
        <button class="small-btn ${changeType === "changed" ? "active-tab" : ""}" data-shayan-change-type="changed">Changed (${counts.changed})</button>
        <button class="small-btn ${changeType === "removed" ? "active-tab" : ""}" data-shayan-change-type="removed">Removed (${counts.removed})</button>
      </div>
      <div class="shayan-change-list">${listBody}</div>
      <div class="run-result-actions">${loadMoreBtn}</div>
    </section>
  `;
}

function renderSummaryArtifacts(summary) {
  const artifacts = summary?.artifacts;
  if (!artifacts || typeof artifacts !== "object") return "";
  const text = JSON.stringify(artifacts, null, 2);
  if (!text) return "";
  return `
    <details class="run-artifacts-box" open>
      <summary>Run artifacts</summary>
      <pre>${escapeHtml(text)}</pre>
    </details>
  `;
}

function toggleButtonModel(task, run) {
  const status = run?.status || "idle";
  if (status === "stopping_graceful") {
    return { icon: "octagon-x", title: "Force stop now", cls: "red" };
  }
  if (status === "stopping_force") {
    return { icon: "octagon-x", title: "Force stopping", cls: "red", disabled: true };
  }
  if (status === "starting" || status === "running") {
    return { icon: "square", title: "Request graceful stop", cls: "active" };
  }
  return {
    icon: window.ManzaraCore.toLucideIcon(task.icon_idle, "play"),
    title: `Start ${task.title}`,
    cls: "",
  };
}

function renderRunList(runs) {
  if (!runs.length) {
    return '<div class="run-row">No runs yet.</div>';
  }
  return runs
    .map((run) => {
      const activeClass = run.run_id === state.selectedRunId ? "selected" : "";
      return `
        <button class="task-run-row ${activeClass}" data-run-id="${run.run_id}">
          <span class="task-run-id">#${run.run_id}</span>
          ${window.ManzaraCore.renderTaskStatusBadge(run, { compact: true })}
          <span class="task-run-time">${escapeHtml(formatDateTime(run.started_at))}</span>
          <span class="task-run-summary">${escapeHtml(runSummaryMessage(run))}</span>
        </button>
      `;
    })
    .join("");
}

function renderRunResult(run) {
  if (!run) return '<div class="run-row">No run selected.</div>';
  const errorText = (run.error_text || "").trim();
  const summary = runSummary(run);
  const highlights = Array.isArray(summary.highlights) ? summary.highlights : [];
  const summaryRows = highlights
    .filter((item) => item && typeof item === "object")
    .map((item) => {
      const label = String(item.label || "").trim();
      const value = String(item.value || "").trim();
      if (!label || !value) return "";
      return `<div><span class="meta-k">${escapeHtml(label)}</span><span class="meta-v">${escapeHtml(value)}</span></div>`;
    })
    .filter(Boolean)
    .join("");
  return `
    <div class="run-result-grid">
      <div><span class="meta-k">Run</span><span class="meta-v">#${run.run_id}</span></div>
      <div><span class="meta-k">Status</span><span class="meta-v">${escapeHtml(run.status)}</span></div>
      <div><span class="meta-k">Started</span><span class="meta-v">${escapeHtml(formatDateTime(run.started_at))}</span></div>
      <div><span class="meta-k">Finished</span><span class="meta-v">${escapeHtml(formatDateTime(run.finished_at))}</span></div>
      <div><span class="meta-k">Duration</span><span class="meta-v">${escapeHtml(runDuration(run))}</span></div>
      <div><span class="meta-k">Exit Code</span><span class="meta-v">${escapeHtml(String(run.exit_code ?? "-"))}</span></div>
    </div>
    <div class="workflow-footnote">${escapeHtml(runSummaryMessage(run))}</div>
    ${summaryRows ? `<div class="run-result-grid">${summaryRows}</div>` : ""}
    ${renderSummaryArtifacts(summary)}
    ${renderShayanChangesSection(run)}
    ${
      errorText
        ? `<div class="run-error-box">${escapeHtml(errorText)}</div>`
        : '<div class="workflow-footnote">No error text for this run.</div>'
    }
    <div class="run-result-actions">
      <button class="small-btn" id="show-run-logs" data-run-id="${run.run_id}">Show logs</button>
    </div>
  `;
}

async function loadShayanChanges(runId, changeType, { append = false } = {}) {
  const keyType = String(changeType || "added");
  const cache = getShayanChangeCache(runId, keyType);
  if (cache.loading) return;
  cache.loading = true;
  cache.error = null;
  const afterChangeId = append ? Number(cache.nextAfterChangeId || 0) : 0;
  if (!append) {
    cache.items = [];
    cache.nextAfterChangeId = 0;
    cache.hasMore = false;
    cache.loaded = false;
  }
  renderTaskDetail(state.payload);
  try {
    const payload = await api(
      `/api/runs/${encodeURIComponent(String(runId))}/shayan-changes?change_type=${encodeURIComponent(
        keyType
      )}&after_change_id=${encodeURIComponent(String(afterChangeId))}&limit=100`
    );
    const items = Array.isArray(payload?.items) ? payload.items : [];
    cache.items = append ? [...cache.items, ...items] : items;
    cache.nextAfterChangeId = Number(payload?.next_after_change_id || 0);
    cache.hasMore = Boolean(payload?.has_more);
    cache.stats = payload?.stats && typeof payload.stats === "object" ? payload.stats : null;
    cache.loaded = true;
  } catch (error) {
    cache.error = error?.message || String(error);
    cache.loaded = true;
  } finally {
    cache.loading = false;
    renderTaskDetail(state.payload);
  }
}

function ensureShayanChangesLoaded(run) {
  if (!isShayanScanDetailsEnabled(run)) return;
  const runId = Number(run?.run_id || 0);
  if (!runId) return;
  const changeType = String(state.selectedShayanChangeType || "added");
  const cache = getShayanChangeCache(runId, changeType);
  if (cache.loaded || cache.loading || cache.error) return;
  loadShayanChanges(runId, changeType).catch((error) => console.error(error));
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

function renderTaskDetail(payload) {
  viewState.set("ready");
  state.payload = payload;
  syncConveyorDraft(payload);
  const task = payload.task;
  const runs = payload.runs || [];
  if (!state.selectedRunId && runs.length) {
    state.selectedRunId = runs[0].run_id;
  }
  const currentRun = selectedRun();
  const buttonModel = toggleButtonModel(task, currentRun);
  ensureCanonicalTaskPath(task.slug || task.task_id);

  document.getElementById("task-title").textContent = task.title;
  document.getElementById("task-subtitle").textContent = `${payload.panel.title} • ${task.task_id} • ${task.task_type}`;
  const backLink = document.getElementById("task-back-link");
  if (backLink && payload.panel?.slug) {
    backLink.href = `/flows/${encodeURIComponent(payload.panel.slug)}`;
    backLink.textContent = "Back to flow";
  }
  document.getElementById("task-stat-grid").innerHTML = `
    <div class="stat"><div class="stat-label">Total Runs</div><div class="stat-value">${payload.stats.total_runs}</div></div>
    <div class="stat"><div class="stat-label">Completed</div><div class="stat-value">${payload.stats.status_counts.completed || 0}</div></div>
    <div class="stat"><div class="stat-label">Failed</div><div class="stat-value">${payload.stats.status_counts.failed || 0}</div></div>
    <div class="stat"><div class="stat-label">Last Success</div><div class="stat-value">${escapeHtml(formatDateTime(payload.stats.last_success_at))}</div></div>
  `;
  document.getElementById("task-run-list").innerHTML = renderRunList(runs);
  document.getElementById("run-result").innerHTML = renderRunResult(currentRun);
  renderConveyor();

  const toggleBtn = document.getElementById("task-toggle-btn");
  toggleBtn.classList.remove("active", "red");
  if (buttonModel.cls) toggleBtn.classList.add(buttonModel.cls);
  toggleBtn.title = buttonModel.title;
  toggleBtn.setAttribute("aria-label", buttonModel.title);
  toggleBtn.disabled = Boolean(buttonModel.disabled);
  toggleBtn.innerHTML = `<i data-lucide="${buttonModel.icon}"></i>`;

  renderGlobalState(payload);
  lucide.createIcons();
  ensureShayanChangesLoaded(currentRun);
}

function renderTaskLoading() {
  viewState.set("loading");
  document.getElementById("task-title").textContent = "Loading task...";
  document.getElementById("task-subtitle").textContent = "";
  document.getElementById("task-stat-grid").innerHTML = "";
  document.getElementById("task-run-list").innerHTML = '<div class="run-row">Loading runs...</div>';
  document.getElementById("run-result").innerHTML = '<div class="run-row">Loading run details...</div>';
  const conveyorStatus = document.getElementById("conveyor-status");
  if (conveyorStatus) conveyorStatus.textContent = "Loading conveyor...";
}

function renderTaskError(error) {
  viewState.set("error");
  const message = String(error?.message || error || "Failed to load task details.");
  const safe = escapeHtml(message);
  document.getElementById("task-title").textContent = "Task unavailable";
  document.getElementById("task-subtitle").textContent = safe;
  document.getElementById("task-stat-grid").innerHTML = "";
  document.getElementById("task-run-list").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
  document.getElementById("run-result").innerHTML = `<div class="run-row">Error: ${safe}</div>`;
  const conveyorStatus = document.getElementById("conveyor-status");
  if (conveyorStatus) conveyorStatus.textContent = `Error: ${message}`;
}

function ensureCanonicalTaskPath(taskPathKey) {
  const canonical = `/tasks/${encodeURIComponent(taskPathKey)}`;
  if (window.location.pathname !== canonical) {
    window.history.replaceState({}, "", canonical);
  }
}

function readTaskIdFromPath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts.length < 2) return null;
  return decodeURIComponent(parts.slice(1).join("/"));
}

async function refreshTaskDetail({ showLoading = false } = {}) {
  if (showLoading) {
    renderTaskLoading();
  }
  try {
    const payload = await api(`/api/tasks/${encodeURIComponent(state.taskId)}?limit=20`);
    renderTaskDetail(payload);
  } catch (error) {
    renderTaskError(error);
    throw error;
  }
}

function queueRefresh(delayMs = 250) {
  window.ManzaraCore.scheduleRefresh(state, refreshTaskDetail, delayMs);
}

function applyOptimisticToggleState() {
  if (!state.payload || !state.payload.task) return;
  const runs = Array.isArray(state.payload.runs) ? state.payload.runs : [];
  state.payload.runs = runs;
  const selected = selectedRun();
  if (!selected) {
    const nowIso = new Date().toISOString();
    const optimisticRun = {
      run_id: null,
      status: "starting",
      started_at: nowIso,
      finished_at: null,
      exit_code: null,
      error_text: null,
      summary: { status: "starting", message: "Starting..." },
    };
    runs.unshift(optimisticRun);
    state.selectedRunId = optimisticRun.run_id;
    return;
  }

  const currentStatus = String(selected.status || "idle");
  if (currentStatus === "starting" || currentStatus === "running") {
    selected.status = "stopping_graceful";
  } else if (currentStatus === "stopping_graceful") {
    selected.status = "stopping_force";
  } else {
    selected.status = "starting";
    selected.started_at = selected.started_at || new Date().toISOString();
    selected.finished_at = null;
    selected.exit_code = null;
    selected.error_text = null;
  }

  if (runs.length > 0 && Number(runs[0]?.run_id || 0) === Number(selected.run_id || 0)) {
    runs[0] = { ...runs[0], ...selected };
  }
}

function applyToggleResult(result) {
  if (!state.payload || !result || typeof result !== "object") return;
  const run = result.run && typeof result.run === "object" ? result.run : null;
  if (!run) return;

  const runs = Array.isArray(state.payload.runs) ? state.payload.runs : [];
  state.payload.runs = runs;
  const runId = Number(run.run_id || 0);
  if (runId > 0) {
    const index = runs.findIndex((item) => Number(item?.run_id || 0) === runId);
    if (index >= 0) {
      runs[index] = { ...runs[index], ...run };
    } else {
      runs.unshift({ ...run });
    }
    state.selectedRunId = runId;
    return;
  }
  if (runs.length > 0) {
    runs[0] = { ...runs[0], ...run };
  }
}

async function toggleTask() {
  const targetTaskId = String(state.payload?.task?.task_id || state.taskId || "").trim();
  if (!targetTaskId) {
    throw new Error("Task id is missing");
  }
  applyOptimisticToggleState();
  if (state.payload) {
    renderTaskDetail(state.payload);
  }
  const result = await api(`/api/tasks/${encodeURIComponent(targetTaskId)}/toggle`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  maybeShowTaskActionError(result);
  applyToggleResult(result);
  if (state.payload) {
    renderTaskDetail(state.payload);
  }
  queueRefresh(0);
}

async function runConveyor(sudoPassword = null) {
  const body = sudoPassword ? { sudo_password: sudoPassword } : {};
  const result = await api("/api/conveyor/run", {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (["sudo_password_required", "sudo_password_invalid"].includes(String(result?.reason))) {
    const password = await window.ManzaraUI.prompt({
      title: "Sudo password required",
      message: String(result.message || "Enter the sudo password needed by this conveyor."),
      inputLabel: "Sudo password",
      inputType: "password",
      acceptLabel: "Run conveyor",
    });
    if (password) return runConveyor(password);
    return result;
  }
  maybeShowTaskActionError(result);
  queueRefresh(0);
  return result;
}

async function stopConveyor() {
  await api("/api/conveyor/stop", { method: "POST", body: JSON.stringify({}) });
  queueRefresh(0);
}

async function clearConveyor() {
  const confirmed = await window.ManzaraUI.confirm({
    title: "Clear conveyor",
    message: "Remove every task from the saved conveyor?",
    acceptLabel: "Clear",
    destructive: true,
  });
  if (!confirmed) return;
  state.conveyorDraft = [];
  await saveConveyorDraft();
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

function initLogViewer() {
  state.logViewer = window.ManzaraCore.createRunLogViewer({
    api,
    dialogNode: document.getElementById("log-dialog"),
    titleNode: document.getElementById("log-title"),
    contentNode: document.getElementById("log-content"),
    tailLimit: 400,
    followLimit: 400,
    backfillLimit: 400,
    pollIntervalMs: 1500,
  });
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
      const relevant = String(payload?.task_id || "") === String(state.payload?.task?.task_id || "");
      if (relevant && window.ManzaraCore.applyTaskEventState(state.payload, payload)) {
        renderTaskDetail(state.payload);
      }
      if (relevant && window.ManzaraCore.eventNeedsReconciliation(payload)) {
        queueRefresh(100);
      }
      const eventType = String(payload?.type || "");
      if (eventType === "task.progress") {
        const item = (state.payload?.conveyor?.items || []).find(
          (entry) => Number(entry.task_run_id || 0) === Number(payload?.run_id || 0)
        );
        if (item && payload?.payload?.progress) {
          item.progress = { ...payload.payload.progress };
          renderConveyor();
        }
      }
      if (eventType.startsWith("conveyor.")) {
        queueRefresh(eventType === "conveyor.updated" ? 0 : 100);
      }
    },
  });
  state.eventStreamController.start();
}

function attachUiHandlers() {

  document.getElementById("task-toggle-btn").addEventListener("click", () => {
    toggleTask().catch((error) => {
      console.error(error);
      window.ManzaraUI.toast(error?.message || String(error), { tone: "error" });
    });
  });

  document.getElementById("conveyor-add-current")?.addEventListener("click", () => {
    const taskId = String(state.payload?.task?.task_id || "");
    if (taskId) addTaskAsStage(taskId);
  });
  document.getElementById("conveyor-run")?.addEventListener("click", () => {
    runConveyor().catch((error) => {
      console.error(error);
      window.ManzaraUI.toast(error?.message || String(error), { tone: "error" });
    });
  });
  document.getElementById("conveyor-stop")?.addEventListener("click", () => {
    stopConveyor().catch((error) => console.error(error));
  });
  document.getElementById("conveyor-clear")?.addEventListener("click", () => {
    clearConveyor().catch((error) => console.error(error));
  });

  const palette = document.getElementById("conveyor-palette");
  palette?.addEventListener("click", (event) => {
    const button = event.target.closest(".conveyor-add-task");
    if (button) addTaskAsStage(button.dataset.taskId);
  });
  palette?.addEventListener("dragstart", (event) => {
    const task = event.target.closest("[data-conveyor-task-id]");
    if (!task) return;
    state.conveyorDrag = { type: "task", taskId: String(task.dataset.conveyorTaskId) };
    event.dataTransfer?.setData("text/plain", `task:${task.dataset.conveyorTaskId}`);
  });

  const stagesRoot = document.getElementById("conveyor-stages");
  stagesRoot?.addEventListener("click", (event) => {
    const control = event.target.closest("button");
    if (!control || control.disabled) return;
    const stageIndex = Number(control.dataset.stageIndex);
    const itemIndex = Number(control.dataset.itemIndex);
    if (control.classList.contains("conveyor-remove-item")) {
      removeConveyorItem(stageIndex, itemIndex);
    } else if (control.classList.contains("conveyor-item-prev")) {
      moveConveyorItem(stageIndex, itemIndex, stageIndex - 1);
    } else if (control.classList.contains("conveyor-item-next")) {
      if (stageIndex + 1 >= state.conveyorDraft.length) {
        const item = state.conveyorDraft[stageIndex]?.items[itemIndex];
        if (!item) return;
        removeConveyorItem(stageIndex, itemIndex);
        state.conveyorDraft.push({ stage_id: conveyorId("stage"), items: [item] });
      } else {
        moveConveyorItem(stageIndex, itemIndex, stageIndex + 1);
      }
    } else if (control.classList.contains("conveyor-stage-up") && stageIndex > 0) {
      [state.conveyorDraft[stageIndex - 1], state.conveyorDraft[stageIndex]] =
        [state.conveyorDraft[stageIndex], state.conveyorDraft[stageIndex - 1]];
    } else if (
      control.classList.contains("conveyor-stage-down")
      && stageIndex < state.conveyorDraft.length - 1
    ) {
      [state.conveyorDraft[stageIndex], state.conveyorDraft[stageIndex + 1]] =
        [state.conveyorDraft[stageIndex + 1], state.conveyorDraft[stageIndex]];
    } else {
      return;
    }
    saveConveyorDraft().catch((error) => console.error(error));
  });
  stagesRoot?.addEventListener("dragstart", (event) => {
    const item = event.target.closest("[data-conveyor-item-id]");
    if (!item) return;
    state.conveyorDrag = { type: "item", itemId: String(item.dataset.conveyorItemId) };
    event.dataTransfer?.setData("text/plain", `item:${item.dataset.conveyorItemId}`);
  });
  stagesRoot?.addEventListener("dragover", (event) => {
    if (event.target.closest("[data-stage-drop-index], [data-new-stage-index]")) {
      event.preventDefault();
    }
  });
  stagesRoot?.addEventListener("drop", (event) => {
    event.preventDefault();
    const drag = state.conveyorDrag;
    state.conveyorDrag = null;
    if (!drag) return;
    const parallelTarget = event.target.closest("[data-stage-drop-index]");
    const rowTarget = event.target.closest("[data-new-stage-index]");
    let draggedItem = null;
    let sourceStageIndex = -1;
    let sourceStageId = null;
    const parallelIndex = parallelTarget ? Number(parallelTarget.dataset.stageDropIndex) : -1;
    const targetStageId = parallelIndex >= 0
      ? String(state.conveyorDraft[parallelIndex]?.stage_id || "")
      : null;
    let newStageIndex = rowTarget ? Number(rowTarget.dataset.newStageIndex) : -1;
    if (drag.type === "item") {
      for (let stageIndex = 0; stageIndex < state.conveyorDraft.length; stageIndex += 1) {
        const itemIndex = state.conveyorDraft[stageIndex].items.findIndex(
          (item) => String(item.item_id) === drag.itemId
        );
        if (itemIndex >= 0) {
          sourceStageIndex = stageIndex;
          sourceStageId = String(state.conveyorDraft[stageIndex].stage_id);
          if (targetStageId && targetStageId === sourceStageId) return;
          [draggedItem] = state.conveyorDraft[stageIndex].items.splice(itemIndex, 1);
          if (!state.conveyorDraft[stageIndex].items.length) {
            state.conveyorDraft.splice(stageIndex, 1);
            if (newStageIndex > sourceStageIndex) newStageIndex -= 1;
          }
          break;
        }
      }
    } else if (drag.type === "task") {
      draggedItem = { item_id: conveyorId("item"), task_id: drag.taskId };
    }
    if (!draggedItem) return;
    if (parallelTarget) {
      const index = state.conveyorDraft.findIndex(
        (stage) => String(stage.stage_id) === targetStageId
      );
      const target = state.conveyorDraft[index];
      if (!target || target.items.some((item) => item.task_id === draggedItem.task_id)) {
        refreshTaskDetail().catch((error) => console.error(error));
        return;
      }
      target.items.push(draggedItem);
    } else if (rowTarget) {
      state.conveyorDraft.splice(newStageIndex, 0, {
        stage_id: conveyorId("stage"),
        items: [draggedItem],
      });
    } else {
      return;
    }
    saveConveyorDraft().catch((error) => console.error(error));
  });

  document.getElementById("stop-all-btn").addEventListener("click", () => {
    stopAll().catch((error) => console.error(error));
  });

  document.getElementById("task-run-list").addEventListener("click", (event) => {
    const row = event.target.closest(".task-run-row");
    if (!row) return;
    const runId = Number(row.dataset.runId || 0);
    if (!runId) return;
    state.selectedRunId = runId;
    renderTaskDetail(state.payload);
  });

  document.getElementById("run-result").addEventListener("click", (event) => {
    const btn = event.target.closest("#show-run-logs");
    if (btn) {
      const runId = Number(btn.dataset.runId || 0);
      if (!runId) return;
      state.logViewer?.open(runId, state.payload?.task?.title || state.taskId).catch((error) =>
        console.error(error)
      );
      return;
    }

    const typeBtn = event.target.closest("[data-shayan-change-type]");
    if (typeBtn) {
      const nextType = String(typeBtn.dataset.shayanChangeType || "").trim();
      if (!nextType) return;
      state.selectedShayanChangeType = nextType;
      const run = selectedRun();
      renderTaskDetail(state.payload);
      ensureShayanChangesLoaded(run);
      return;
    }

    const moreBtn = event.target.closest("[data-load-shayan-more]");
    if (moreBtn) {
      const run = selectedRun();
      if (!run) return;
      const runId = Number(run.run_id || 0);
      if (!runId) return;
      loadShayanChanges(runId, state.selectedShayanChangeType, { append: true }).catch((error) =>
        console.error(error)
      );
    }
  });

  document.getElementById("close-logs").addEventListener("click", () => {
    state.logViewer?.close();
  });
  document.getElementById("log-dialog").addEventListener("close", () => {
    state.logViewer?.close({ closeDialog: false });
  });

  document.getElementById("copy-logs").addEventListener("click", async () => {
    const text = document.getElementById("log-content").textContent;
    await navigator.clipboard.writeText(text || "");
  });
}

async function bootstrap() {
  state.taskId = readTaskIdFromPath();
  if (!state.taskId) {
    throw new Error("Task id is missing in URL");
  }

  initSoundNotifier();
  initLogViewer();
  window.addEventListener("beforeunload", () => {
    teardownSoundNotifier();
    state.logViewer?.destroy();
    state.logViewer = null;
    if (state.eventStreamController) {
      state.eventStreamController.stop();
      state.eventStreamController = null;
    }

  });
  attachUiHandlers();
  await refreshTaskDetail({ showLoading: true });
  setupEventStream();
}

bootstrap().catch((error) => {
  console.error(error);
  window.ManzaraUI.toast(error.message || String(error), { tone: "error" });
});
