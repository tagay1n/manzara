(function () {
  "use strict";

  const STORAGE_KEY = "manzara.task-review.v1";
  const VERSION = 1;
  const FIRST_VISIT_WINDOW_MS = 24 * 60 * 60 * 1000;
  const REVIEWABLE_STATUSES = new Set(["completed", "failed"]);

  function emptyState() {
    return {
      version: VERSION,
      catalog_initialized: false,
      opened: {},
    };
  }

  function positiveRunId(value) {
    const runId = Number(value || 0);
    return Number.isInteger(runId) && runId > 0 ? runId : 0;
  }

  function normalizeState(value) {
    if (!value || typeof value !== "object" || Number(value.version) !== VERSION) {
      return emptyState();
    }
    const opened = {};
    if (value.opened && typeof value.opened === "object") {
      for (const [taskId, rawRunId] of Object.entries(value.opened)) {
        const runId = positiveRunId(rawRunId);
        if (taskId && runId) opened[String(taskId)] = runId;
      }
    }
    return {
      version: VERSION,
      catalog_initialized: value.catalog_initialized === true,
      opened,
    };
  }

  function reviewableRun(task) {
    const taskId = String(task?.task_id || "");
    const run = task?.run;
    const runId = positiveRunId(run?.run_id);
    const status = String(run?.status || "");
    if (!taskId || !runId || !REVIEWABLE_STATUSES.has(status)) return null;
    return { taskId, runId, status, finishedAt: run?.finished_at };
  }

  function createStore(options = {}) {
    let storage = options.storage;
    if (storage === undefined) {
      try {
        storage = window.localStorage;
      } catch (_error) {
        storage = null;
      }
    }
    const now = typeof options.now === "function" ? options.now : () => Date.now();
    let state = emptyState();

    try {
      const raw = storage?.getItem?.(STORAGE_KEY);
      state = raw ? normalizeState(JSON.parse(raw)) : emptyState();
    } catch (_error) {
      state = emptyState();
    }

    function persist() {
      try {
        storage?.setItem?.(STORAGE_KEY, JSON.stringify(state));
      } catch (_error) {
        // Browser privacy settings may disable storage; in-memory state still works.
      }
    }

    function syncCatalog(tasks) {
      const catalogTasks = Array.isArray(tasks) ? tasks : [];
      const currentTaskIds = new Set(
        catalogTasks.map((task) => String(task?.task_id || "")).filter(Boolean),
      );

      if (!state.catalog_initialized) {
        const cutoff = Number(now()) - FIRST_VISIT_WINDOW_MS;
        for (const task of catalogTasks) {
          const terminal = reviewableRun(task);
          if (!terminal) continue;
          const finishedAt = Date.parse(String(terminal.finishedAt || ""));
          if (!Number.isFinite(finishedAt) || finishedAt < cutoff) {
            state.opened[terminal.taskId] = terminal.runId;
          }
        }
        state.catalog_initialized = true;
      }

      for (const taskId of Object.keys(state.opened)) {
        if (!currentTaskIds.has(taskId)) delete state.opened[taskId];
      }
      persist();
    }

    function isUnread(task) {
      const terminal = reviewableRun(task);
      return Boolean(
        terminal && positiveRunId(state.opened[terminal.taskId]) !== terminal.runId,
      );
    }

    function markOpened(taskId, runId) {
      const normalizedTaskId = String(taskId || "");
      const normalizedRunId = positiveRunId(runId);
      if (!normalizedTaskId || !normalizedRunId) return false;
      state.opened[normalizedTaskId] = normalizedRunId;
      persist();
      return true;
    }

    function markRunOpened(taskId, run) {
      if (!REVIEWABLE_STATUSES.has(String(run?.status || ""))) return false;
      return markOpened(taskId, run?.run_id);
    }

    return {
      isUnread,
      markOpened,
      markRunOpened,
      syncCatalog,
    };
  }

  window.ManzaraTaskReview = {
    STORAGE_KEY,
    createStore,
  };
})();
