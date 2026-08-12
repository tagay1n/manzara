(function () {
  "use strict";

  function createController(options = {}) {
    const api = options.api;
    const getPayload = options.getPayload;
    const refresh = options.refresh;
    let draft = null;
    let saving = false;
    let drag = null;

    const escapeHtml = (value) => window.ManzaraCore.escapeHtml(value);
    const cssName = (value) => window.ManzaraCore.cssName(value);

    function makeId(prefix) {
      if (globalThis.crypto?.randomUUID) return `${prefix}-${globalThis.crypto.randomUUID()}`;
      return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    }

    function payload() {
      return getPayload?.() || {};
    }

    function taskMap() {
      return new Map(
        (payload().conveyor?.available_tasks || []).map((task) => [String(task.task_id), task])
      );
    }

    function runActive() {
      return ["starting", "running"].includes(String(payload().conveyor?.run?.status || ""));
    }

    function lockedItemIds() {
      if (!runActive()) return new Set();
      return new Set(
        (payload().conveyor?.items || [])
          .filter((item) => String(item.status || "pending") !== "pending")
          .map((item) => String(item.item_id))
      );
    }

    function sync(nextPayload = payload()) {
      const stages = nextPayload?.conveyor?.definition?.stages;
      draft = JSON.parse(JSON.stringify(Array.isArray(stages) ? stages : []));
    }

    function itemState(itemId) {
      return (payload().conveyor?.items || []).find(
        (item) => String(item.item_id) === String(itemId)
      ) || null;
    }

    function statusText() {
      const run = payload().conveyor?.run;
      if (!run) return "Drag task badges from the catalog below. Tasks in one row run in parallel.";
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

    function itemHtml(item, stageIndex, itemIndex, locked, tasks) {
      const task = tasks.get(String(item.task_id)) || {
        title: item.task_id,
        panel_title: "Unknown",
      };
      const runtime = itemState(item.item_id);
      const status = String(runtime?.status || "pending");
      const progress = runtime?.progress && typeof runtime.progress === "object"
        ? runtime.progress
        : {};
      const progressModel = window.ManzaraCore.taskStatusBadgeModel({ status, progress });
      const progressLabel = progressModel.determinate
        ? ` • ${progressModel.current} / ${progressModel.total} · ${progressModel.percent}%`
        : "";
      const resultLabel = runtime?.meaningful === false ? " • no-op" : "";
      const progressHtml = progressModel.determinate
        ? `<div class="conveyor-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progressModel.percent}" aria-label="${escapeHtml(task.title)}: ${progressModel.current} of ${progressModel.total}"><span style="width:${progressModel.percent}%"></span></div>`
        : "";
      return `
        <article class="conveyor-item conveyor-status-${cssName(status)} ${locked ? "is-locked" : ""}"
          draggable="${locked ? "false" : "true"}" data-conveyor-item-id="${escapeHtml(item.item_id)}">
          <div class="conveyor-item-main">
            <strong>${escapeHtml(task.title)}</strong>
            <span>${escapeHtml(task.panel_title || task.panel_id || "")} • ${escapeHtml(status)}${resultLabel}${escapeHtml(progressLabel)}</span>
          </div>
          ${progressHtml}
          <div class="conveyor-item-actions">
            <button type="button" class="icon-btn quiet conveyor-item-prev" data-stage-index="${stageIndex}" data-item-index="${itemIndex}" ${locked || stageIndex === 0 ? "disabled" : ""} aria-label="Move to previous row" title="Move to previous row"><i data-lucide="arrow-up-to-line"></i></button>
            <button type="button" class="icon-btn quiet conveyor-item-next" data-stage-index="${stageIndex}" data-item-index="${itemIndex}" ${locked ? "disabled" : ""} aria-label="Move to next row" title="Move to next row"><i data-lucide="arrow-down-to-line"></i></button>
            <button type="button" class="icon-btn quiet conveyor-remove-item" data-stage-index="${stageIndex}" data-item-index="${itemIndex}" ${locked ? "disabled" : ""} aria-label="Remove task" title="Remove task"><i data-lucide="x"></i></button>
          </div>
        </article>
      `;
    }

    function stagesHtml(stages, tasks, lockedIds) {
      if (!stages.length) {
        return '<div class="conveyor-new-row-drop" data-new-stage-index="0">Drag a task badge here</div>';
      }
      const lockedStageIndexes = stages
        .map((stage, index) => stage.items.some((item) => lockedIds.has(String(item.item_id))) ? index : -1)
        .filter((index) => index >= 0);
      const minimumEditableIndex = lockedStageIndexes.length
        ? Math.max(...lockedStageIndexes) + 1
        : 0;
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
              ${stage.items.map((item, itemIndex) => itemHtml(item, stageIndex, itemIndex, locked, tasks)).join("")}
            </div>
          </section>
        `;
      }).join("") + `<div class="conveyor-new-row-drop" data-new-stage-index="${stages.length}">Drop for a final sequential row</div>`;
    }

    function render() {
      const root = document.getElementById("conveyor-stages");
      if (!root) return;
      if (!draft) sync();
      const lockedIds = lockedItemIds();
      root.innerHTML = stagesHtml(draft || [], taskMap(), lockedIds);
      root.classList.toggle("is-empty", !draft?.length);
      const statusNode = document.getElementById("conveyor-status");
      if (statusNode) statusNode.textContent = statusText();
      const active = runActive();
      const runButton = document.getElementById("conveyor-run");
      const stopButton = document.getElementById("conveyor-stop");
      const clearButton = document.getElementById("conveyor-clear");
      if (runButton) {
        runButton.hidden = active;
        runButton.disabled = !draft?.length || saving;
      }
      if (stopButton) stopButton.hidden = !active;
      if (clearButton) clearButton.disabled = saving || lockedIds.size > 0;
      lucide.createIcons();
    }

    async function save() {
      if (saving) return;
      saving = true;
      render();
      try {
        const revision = Number(payload().conveyor?.definition?.revision || 0);
        const result = await api("/api/conveyor", {
          method: "PUT",
          body: JSON.stringify({ revision, stages: draft || [] }),
        });
        payload().conveyor.definition = result.definition;
        sync();
      } catch (error) {
        window.ManzaraUI.toast(error?.message || String(error), { tone: "error" });
        await refresh();
      } finally {
        saving = false;
        render();
      }
    }

    function addTaskAsStage(taskId, stageIndex = null) {
      if (!taskMap().has(String(taskId))) return;
      const stage = {
        stage_id: makeId("stage"),
        items: [{ item_id: makeId("item"), task_id: String(taskId) }],
      };
      const index = stageIndex === null ? draft.length : Number(stageIndex);
      draft.splice(index, 0, stage);
      save().catch((error) => console.error(error));
    }

    function removeItem(stageIndex, itemIndex) {
      const stage = draft?.[stageIndex];
      if (!stage) return;
      stage.items.splice(itemIndex, 1);
      if (!stage.items.length) draft.splice(stageIndex, 1);
    }

    function moveItem(stageIndex, itemIndex, targetStageIndex) {
      const source = draft?.[stageIndex];
      const target = draft?.[targetStageIndex];
      if (!source || !target) return;
      const [item] = source.items.splice(itemIndex, 1);
      if (!item || target.items.some((entry) => entry.task_id === item.task_id)) {
        if (item) source.items.splice(itemIndex, 0, item);
        return;
      }
      target.items.push(item);
      if (!source.items.length) draft.splice(stageIndex, 1);
    }

    async function run(sudoPassword = null) {
      const result = await api("/api/conveyor/run", {
        method: "POST",
        body: JSON.stringify(sudoPassword ? { sudo_password: sudoPassword } : {}),
      });
      if (["sudo_password_required", "sudo_password_invalid"].includes(String(result?.reason))) {
        const password = await window.ManzaraUI.prompt({
          title: "Sudo password required",
          message: String(result.message || "Enter the sudo password needed by this conveyor."),
          inputLabel: "Sudo password",
          inputType: "password",
          acceptLabel: "Run conveyor",
        });
        if (password) return run(password);
        return result;
      }
      window.ManzaraUI?.reportTaskActionResult(result);
      refresh();
      return result;
    }

    async function stop() {
      await api("/api/conveyor/stop", { method: "POST", body: JSON.stringify({}) });
      refresh();
    }

    async function clear() {
      const confirmed = await window.ManzaraUI.confirm({
        title: "Clear conveyor",
        message: "Remove every task from the saved conveyor?",
        acceptLabel: "Clear",
        destructive: true,
      });
      if (!confirmed) return;
      draft = [];
      await save();
    }

    function attach() {
      document.getElementById("conveyor-run")?.addEventListener("click", () => {
        run().catch((error) => window.ManzaraUI.toast(error?.message || String(error), { tone: "error" }));
      });
      document.getElementById("conveyor-stop")?.addEventListener("click", () => {
        stop().catch((error) => console.error(error));
      });
      document.getElementById("conveyor-clear")?.addEventListener("click", () => {
        clear().catch((error) => console.error(error));
      });

      const catalog = document.getElementById("task-flow-grid");
      catalog?.addEventListener("dragstart", (event) => {
        const task = event.target.closest("[data-conveyor-task-id]");
        if (!task) return;
        drag = { type: "task", taskId: String(task.dataset.conveyorTaskId) };
        if (event.dataTransfer) event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer?.setData("text/plain", `task:${task.dataset.conveyorTaskId}`);
      });

      const stagesRoot = document.getElementById("conveyor-stages");
      stagesRoot?.addEventListener("click", (event) => {
        const control = event.target.closest("button");
        if (!control || control.disabled) return;
        const stageIndex = Number(control.dataset.stageIndex);
        const itemIndex = Number(control.dataset.itemIndex);
        if (control.classList.contains("conveyor-remove-item")) {
          removeItem(stageIndex, itemIndex);
        } else if (control.classList.contains("conveyor-item-prev")) {
          moveItem(stageIndex, itemIndex, stageIndex - 1);
        } else if (control.classList.contains("conveyor-item-next")) {
          if (stageIndex + 1 >= draft.length) {
            const item = draft[stageIndex]?.items[itemIndex];
            if (!item) return;
            removeItem(stageIndex, itemIndex);
            draft.push({ stage_id: makeId("stage"), items: [item] });
          } else {
            moveItem(stageIndex, itemIndex, stageIndex + 1);
          }
        } else if (control.classList.contains("conveyor-stage-up") && stageIndex > 0) {
          [draft[stageIndex - 1], draft[stageIndex]] = [draft[stageIndex], draft[stageIndex - 1]];
        } else if (
          control.classList.contains("conveyor-stage-down")
          && stageIndex < draft.length - 1
        ) {
          [draft[stageIndex], draft[stageIndex + 1]] = [draft[stageIndex + 1], draft[stageIndex]];
        } else {
          return;
        }
        save().catch((error) => console.error(error));
      });
      stagesRoot?.addEventListener("dragstart", (event) => {
        const item = event.target.closest("[data-conveyor-item-id]");
        if (!item) return;
        drag = { type: "item", itemId: String(item.dataset.conveyorItemId) };
        event.dataTransfer?.setData("text/plain", `item:${item.dataset.conveyorItemId}`);
      });
      stagesRoot?.addEventListener("dragover", (event) => {
        if (event.target.closest("[data-stage-drop-index], [data-new-stage-index]")) {
          event.preventDefault();
        }
      });
      stagesRoot?.addEventListener("drop", (event) => {
        event.preventDefault();
        const currentDrag = drag;
        drag = null;
        if (!currentDrag) return;
        const parallelTarget = event.target.closest("[data-stage-drop-index]");
        const rowTarget = event.target.closest("[data-new-stage-index]");
        let draggedItem = null;
        let sourceStageIndex = -1;
        let sourceStageId = null;
        const parallelIndex = parallelTarget ? Number(parallelTarget.dataset.stageDropIndex) : -1;
        const targetStageId = parallelIndex >= 0 ? String(draft[parallelIndex]?.stage_id || "") : null;
        let newStageIndex = rowTarget ? Number(rowTarget.dataset.newStageIndex) : -1;
        if (currentDrag.type === "item") {
          for (let stageIndex = 0; stageIndex < draft.length; stageIndex += 1) {
            const itemIndex = draft[stageIndex].items.findIndex(
              (item) => String(item.item_id) === currentDrag.itemId
            );
            if (itemIndex >= 0) {
              sourceStageIndex = stageIndex;
              sourceStageId = String(draft[stageIndex].stage_id);
              if (targetStageId && targetStageId === sourceStageId) return;
              [draggedItem] = draft[stageIndex].items.splice(itemIndex, 1);
              if (!draft[stageIndex].items.length) {
                draft.splice(stageIndex, 1);
                if (newStageIndex > sourceStageIndex) newStageIndex -= 1;
              }
              break;
            }
          }
        } else if (currentDrag.type === "task") {
          draggedItem = { item_id: makeId("item"), task_id: currentDrag.taskId };
        }
        if (!draggedItem) return;
        if (parallelTarget) {
          const index = draft.findIndex((stage) => String(stage.stage_id) === targetStageId);
          const target = draft[index];
          if (!target || target.items.some((item) => item.task_id === draggedItem.task_id)) {
            refresh();
            return;
          }
          target.items.push(draggedItem);
        } else if (rowTarget) {
          draft.splice(newStageIndex, 0, {
            stage_id: makeId("stage"),
            items: [draggedItem],
          });
        } else {
          return;
        }
        save().catch((error) => console.error(error));
      });
    }

    function handleEvent(eventPayload) {
      if (String(eventPayload?.type || "") !== "task.progress") return;
      const item = (payload().conveyor?.items || []).find(
        (entry) => Number(entry.task_run_id || 0) === Number(eventPayload?.run_id || 0)
      );
      if (item && eventPayload?.payload?.progress) {
        item.progress = { ...eventPayload.payload.progress };
        render();
      }
    }

    function setLoading() {
      const status = document.getElementById("conveyor-status");
      if (status) status.textContent = "Loading conveyor...";
    }

    function setError(error) {
      const status = document.getElementById("conveyor-status");
      if (status) status.textContent = `Error: ${String(error?.message || error)}`;
    }

    return { attach, handleEvent, render, setError, setLoading, sync };
  }

  window.ManzaraConveyor = { createController };
})();
