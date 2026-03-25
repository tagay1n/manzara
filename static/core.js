(() => {
  const DEFAULT_EVENT_TYPES = [
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

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    return date;
  }

  function formatDateTime(value, options = {}) {
    const date = parseDate(value);
    if (!date) return "-";
    const includeSeconds = Boolean(options.includeSeconds);
    const includeZone = options.includeZone === undefined ? true : Boolean(options.includeZone);
    return new Intl.DateTimeFormat("en-GB", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: includeSeconds ? "2-digit" : undefined,
      hour12: false,
      timeZoneName: includeZone ? "short" : undefined,
    }).format(date);
  }

  function formatTime(value, options = {}) {
    const date = parseDate(value);
    if (!date) return "-";
    const includeSeconds = Boolean(options.includeSeconds);
    const includeZone = options.includeZone === undefined ? true : Boolean(options.includeZone);
    return new Intl.DateTimeFormat("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      second: includeSeconds ? "2-digit" : undefined,
      hour12: false,
      timeZoneName: includeZone ? "short" : undefined,
    }).format(date);
  }

  function formatEventBanner(payload) {
    const eventType = String(payload?.type || "event");
    return `Last event: ${eventType} @ ${formatTime(payload?.ts, { includeZone: true })}`;
  }

  function formatGlobalStatus(activeTasks, activeWorkflows) {
    return `Tasks: ${Number(activeTasks || 0)} • Flows: ${Number(activeWorkflows || 0)}`;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function setStatusMessage(node, text, options = {}) {
    if (!node) return;
    const isError = Boolean(options.error);
    node.textContent = String(text ?? "");
    if (node.classList) {
      node.classList.toggle("library-status-error", isError);
    }
  }

  function renderRunRowMessage(text, options = {}) {
    const isError = Boolean(options.error);
    const prefix = isError ? "Error: " : "";
    return `<div class="run-row">${prefix}${escapeHtml(String(text ?? ""))}</div>`;
  }

  function renderWorkflowFootnoteMessage(text, options = {}) {
    const isError = Boolean(options.error);
    const classes = isError ? "workflow-footnote library-status-error" : "workflow-footnote";
    return `<div class="${classes}">${escapeHtml(String(text ?? ""))}</div>`;
  }

  function renderLoadingTableRow(colSpan, text) {
    const safeColSpan = Math.max(1, Math.trunc(Number(colSpan) || 1));
    return `<tr><td colspan="${safeColSpan}">${escapeHtml(String(text ?? ""))}</td></tr>`;
  }

  function applyPaginationControls(options = {}) {
    const page = Math.max(1, Math.trunc(Number(options.page) || 1));
    const totalPages = Math.max(1, Math.trunc(Number(options.totalPages) || 1));
    const labelPrefix = String(options.labelPrefix || "Page");
    if (options.labelNode) {
      options.labelNode.textContent = `${labelPrefix} ${page} / ${totalPages}`;
    }
    if (options.prevNode) {
      options.prevNode.disabled = page <= 1;
    }
    if (options.nextNode) {
      options.nextNode.disabled = page >= totalPages;
    }
  }

  function cssName(name, fallback = "unknown") {
    const value = String(name || "").trim().toLowerCase();
    if (!value) return fallback;
    return value.replace(/[^a-z0-9_-]+/g, "-");
  }

  const VIEW_STATES = {
    LOADING: "loading",
    READY: "ready",
    EMPTY: "empty",
    ERROR: "error",
  };

  const VIEW_STATE_VALUES = new Set(Object.values(VIEW_STATES));

  function normalizeViewState(value, fallback = VIEW_STATES.LOADING) {
    const next = String(value || "").trim().toLowerCase();
    if (VIEW_STATE_VALUES.has(next)) return next;
    return String(fallback || VIEW_STATES.LOADING);
  }

  function attachViewState(state, initial = VIEW_STATES.LOADING) {
    const target = state && typeof state === "object" ? state : {};
    target.viewState = normalizeViewState(initial);
    return {
      get() {
        return normalizeViewState(target.viewState);
      },
      set(next) {
        const value = normalizeViewState(next, this.get());
        target.viewState = value;
        return value;
      },
      is(next) {
        return this.get() === normalizeViewState(next);
      },
    };
  }

  function isActiveStatus(status) {
    const value = String(status || "");
    return (
      value === "starting" ||
      value === "running" ||
      value === "stopping_graceful" ||
      value === "stopping_force"
    );
  }

  function applyStopAllButton(button, stopAllState) {
    if (!button) return;
    const state = String(stopAllState || "disabled");
    const armed = state === "armed";
    button.disabled = state === "disabled";
    button.classList.remove("amber", "red");
    if (armed) {
      button.classList.add("red");
      button.title = "Force stop all running tasks";
      button.setAttribute("aria-label", "Force stop all running tasks");
      button.innerHTML = '<i data-lucide="octagon-x"></i>';
      return;
    }
    button.classList.add("amber");
    button.title = "Graceful stop all running tasks";
    button.setAttribute("aria-label", "Graceful stop all running tasks");
    button.innerHTML = '<i data-lucide="square"></i>';
  }

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

  function createTabController(options = {}) {
    const tabs = Array.isArray(options.tabs)
      ? options.tabs.map((tab) => String(tab || "")).filter(Boolean)
      : [];
    const validTabs = new Set(tabs);
    const getActiveTab = typeof options.getActiveTab === "function"
      ? options.getActiveTab
      : () => "";
    const setActiveTab = typeof options.setActiveTab === "function"
      ? options.setActiveTab
      : () => {};

    function apply() {
      for (const tab of tabs) {
        const isActive = String(getActiveTab() || "") === tab;
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

    function select(tab) {
      const value = String(tab || "");
      if (!validTabs.has(value)) return false;
      setActiveTab(value);
      apply();
      return true;
    }

    return {
      apply,
      select,
      isValid(tab) {
        return validTabs.has(String(tab || ""));
      },
      tabs: [...tabs],
    };
  }

  function createSseController(options = {}) {
    const eventTypes = Array.isArray(options.eventTypes) && options.eventTypes.length
      ? [...options.eventTypes]
      : [...DEFAULT_EVENT_TYPES];
    const reconnectDelayMs = Number(options.reconnectDelayMs || 1500);
    const getCursor = typeof options.getCursor === "function" ? options.getCursor : () => 0;
    const setCursor = typeof options.setCursor === "function" ? options.setCursor : () => {};
    const onEvent = typeof options.onEvent === "function" ? options.onEvent : () => {};
    const onOpen = typeof options.onOpen === "function" ? options.onOpen : () => {};
    const onError = typeof options.onError === "function" ? options.onError : () => {};
    const streamPath = String(options.streamPath || "/api/events/stream");

    let stopped = false;
    let stream = null;
    let reconnectTimer = null;

    function clearReconnectTimer() {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    }

    function scheduleReconnect() {
      if (stopped || reconnectTimer) return;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, Number.isFinite(reconnectDelayMs) && reconnectDelayMs > 0 ? reconnectDelayMs : 1500);
    }

    function streamUrl() {
      const cursor = Number(getCursor() || 0);
      if (Number.isFinite(cursor) && cursor > 0) {
        return `${streamPath}?after_event_id=${encodeURIComponent(String(cursor))}`;
      }
      return streamPath;
    }

    function updateCursor(event, payload) {
      const current = Number(getCursor() || 0);
      const fromSse = Number(String(event?.lastEventId || ""));
      const fromPayload = Number(payload?.event_id || payload?.id || 0);
      const candidate = Number.isFinite(fromSse) && fromSse > 0 ? fromSse : fromPayload;
      if (Number.isFinite(candidate) && candidate > current) {
        setCursor(candidate);
      }
    }

    function connect() {
      if (stopped) return;
      if (typeof EventSource !== "function") {
        throw new Error("EventSource is not available in this environment");
      }

      if (stream) {
        stream.close();
        stream = null;
      }

      const next = new EventSource(streamUrl());
      stream = next;

      eventTypes.forEach((eventType) => {
        next.addEventListener(eventType, (event) => {
          let payload;
          try {
            payload = JSON.parse(String(event.data || "{}"));
          } catch (_error) {
            return;
          }
          updateCursor(event, payload);
          onEvent(payload, event);
        });
      });

      next.onopen = () => {
        clearReconnectTimer();
        onOpen();
      };

      next.onerror = () => {
        onError();
        if (stream === next) {
          next.close();
          stream = null;
          scheduleReconnect();
        }
      };
    }

    function start() {
      stopped = false;
      connect();
    }

    function stop() {
      stopped = true;
      clearReconnectTimer();
      if (stream) {
        stream.close();
        stream = null;
      }
    }

    return {
      start,
      stop,
      getCurrentCursor() {
        return Number(getCursor() || 0);
      },
    };
  }

  function createRunLogViewer(options = {}) {
    const contentNode = options.contentNode || null;
    const titleNode = options.titleNode || null;
    const dialogNode = options.dialogNode || null;
    const fetchApi = typeof options.api === "function" ? options.api : api;
    const pollIntervalMs = Number(options.pollIntervalMs || 1500);
    const tailLimit = Math.max(1, Math.trunc(Number(options.tailLimit || 400)));
    const followLimit = Math.max(1, Math.trunc(Number(options.followLimit || 400)));
    const backfillLimit = Math.max(1, Math.trunc(Number(options.backfillLimit || 400)));
    const nearBottomThresholdPx = Math.max(0, Math.trunc(Number(options.nearBottomThresholdPx || 36)));
    const backfillTriggerPx = Math.max(0, Math.trunc(Number(options.backfillTriggerPx || 48)));

    let activeRunId = null;
    let nextAfterLogId = 0;
    let nextBeforeLogId = 0;
    let hasMoreBefore = false;
    let pollTimer = null;
    let loadingFollow = false;
    let loadingBackfill = false;
    const entryIds = new Set();
    const entries = [];

    function clearPollTimer() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function normalizedLineValue(raw) {
      return String(raw ?? "");
    }

    function isNearBottom() {
      if (!contentNode) return true;
      const distance = contentNode.scrollHeight - (contentNode.scrollTop + contentNode.clientHeight);
      return distance <= nearBottomThresholdPx;
    }

    function renderAll() {
      if (!contentNode) return;
      if (!entries.length) {
        contentNode.textContent = "";
        return;
      }
      contentNode.textContent = `${entries.map((item) => item.line).join("\n")}\n`;
    }

    function appendEntries(lines) {
      if (!Array.isArray(lines) || !lines.length || !contentNode) return 0;
      const appended = [];
      for (const item of lines) {
        const logId = Number(item?.log_id || 0);
        if (!Number.isFinite(logId) || logId <= 0 || entryIds.has(logId)) continue;
        entryIds.add(logId);
        const entry = {
          log_id: logId,
          line: normalizedLineValue(item?.line),
        };
        entries.push(entry);
        appended.push(entry.line);
      }
      if (!appended.length) return 0;
      const suffix = `${appended.join("\n")}\n`;
      contentNode.textContent += suffix;
      return appended.length;
    }

    function prependEntries(lines) {
      if (!Array.isArray(lines) || !lines.length || !contentNode) return 0;
      const chunk = [];
      for (const item of lines) {
        const logId = Number(item?.log_id || 0);
        if (!Number.isFinite(logId) || logId <= 0 || entryIds.has(logId)) continue;
        entryIds.add(logId);
        chunk.push({
          log_id: logId,
          line: normalizedLineValue(item?.line),
        });
      }
      if (!chunk.length) return 0;
      entries.unshift(...chunk);
      renderAll();
      return chunk.length;
    }

    function applyCursorPayload(payload, fallback = {}) {
      const fallbackAfter = Number(fallback.after || 0);
      const fallbackBefore = Number(fallback.before || 0);
      const nextAfter = Number(payload?.next_after_log_id || 0);
      const nextBefore = Number(payload?.next_before_log_id || 0);
      if (nextAfter > 0) {
        nextAfterLogId = Math.max(nextAfterLogId, nextAfter);
      } else if (fallbackAfter > 0) {
        nextAfterLogId = Math.max(nextAfterLogId, fallbackAfter);
      }
      if (nextBefore > 0) {
        nextBeforeLogId = nextBeforeLogId > 0 ? Math.min(nextBeforeLogId, nextBefore) : nextBefore;
      } else if (fallbackBefore > 0) {
        nextBeforeLogId = nextBeforeLogId > 0 ? Math.min(nextBeforeLogId, fallbackBefore) : fallbackBefore;
      } else if (entries.length) {
        nextBeforeLogId = Number(entries[0]?.log_id || 0);
      }
      if (typeof payload?.has_more_before === "boolean") {
        hasMoreBefore = payload.has_more_before;
      } else if (!entries.length) {
        hasMoreBefore = false;
      }
    }

    async function fetchChunk(params = {}) {
      if (!activeRunId) return null;
      const query = new URLSearchParams();
      query.set("limit", String(Math.max(1, Math.trunc(Number(params.limit || followLimit)))));
      if (params.tail) {
        query.set("tail", "true");
      }
      if (Number(params.afterLogId || 0) > 0) {
        query.set("after_log_id", String(Math.trunc(Number(params.afterLogId))));
      }
      if (Number(params.beforeLogId || 0) > 0) {
        query.set("before_log_id", String(Math.trunc(Number(params.beforeLogId))));
      }
      return fetchApi(`/api/runs/${activeRunId}/logs?${query.toString()}`);
    }

    async function pollFollow() {
      if (!activeRunId || loadingFollow) return;
      loadingFollow = true;
      try {
        const wasNearBottom = isNearBottom();
        const payload = await fetchChunk({
          afterLogId: nextAfterLogId,
          limit: followLimit,
        });
        if (!payload) return;
        appendEntries(payload.lines);
        applyCursorPayload(payload, { after: nextAfterLogId, before: nextBeforeLogId });
        if (wasNearBottom && contentNode) {
          contentNode.scrollTop = contentNode.scrollHeight;
        }
      } finally {
        loadingFollow = false;
      }
    }

    async function loadOlder() {
      if (!activeRunId || loadingBackfill) return;
      if (!hasMoreBefore || nextBeforeLogId <= 0) return;
      loadingBackfill = true;
      try {
        const previousHeight = contentNode ? contentNode.scrollHeight : 0;
        const previousTop = contentNode ? contentNode.scrollTop : 0;
        const payload = await fetchChunk({
          beforeLogId: nextBeforeLogId,
          limit: backfillLimit,
        });
        if (!payload) return;
        prependEntries(payload.lines);
        applyCursorPayload(payload, { before: nextBeforeLogId, after: nextAfterLogId });
        if (contentNode) {
          const delta = contentNode.scrollHeight - previousHeight;
          contentNode.scrollTop = previousTop + delta;
        }
      } finally {
        loadingBackfill = false;
      }
    }

    function onScroll() {
      if (!contentNode || !activeRunId) return;
      if (contentNode.scrollTop > backfillTriggerPx) return;
      loadOlder().catch((error) => console.error(error));
    }

    if (contentNode && typeof contentNode.addEventListener === "function") {
      contentNode.addEventListener("scroll", onScroll);
    }

    function resetState() {
      entryIds.clear();
      entries.splice(0, entries.length);
      nextAfterLogId = 0;
      nextBeforeLogId = 0;
      hasMoreBefore = false;
      if (contentNode) {
        contentNode.textContent = "";
      }
    }

    async function open(runId, taskTitle = "Task") {
      activeRunId = Number(runId || 0);
      if (!Number.isFinite(activeRunId) || activeRunId <= 0) {
        throw new Error("run_id must be a positive integer");
      }
      clearPollTimer();
      resetState();
      if (titleNode) {
        titleNode.textContent = `Logs • ${taskTitle} • run ${activeRunId}`;
      }
      if (dialogNode && !dialogNode.open && typeof dialogNode.showModal === "function") {
        dialogNode.showModal();
      }

      const payload = await fetchChunk({ tail: true, limit: tailLimit });
      if (payload) {
        appendEntries(payload.lines);
        applyCursorPayload(payload);
      }
      if (contentNode) {
        contentNode.scrollTop = contentNode.scrollHeight;
      }

      pollTimer = setInterval(() => {
        pollFollow().catch((error) => console.error(error));
      }, Number.isFinite(pollIntervalMs) && pollIntervalMs > 0 ? pollIntervalMs : 1500);
    }

    function close(options = {}) {
      clearPollTimer();
      activeRunId = null;
      if (options.keepContent !== true) {
        resetState();
      }
      if (options.closeDialog !== false && dialogNode && dialogNode.open) {
        dialogNode.close();
      }
    }

    function destroy() {
      close();
      if (contentNode && typeof contentNode.removeEventListener === "function") {
        contentNode.removeEventListener("scroll", onScroll);
      }
    }

    return {
      open,
      close,
      destroy,
      loadOlder,
      pollFollow,
      getState() {
        return {
          activeRunId,
          nextAfterLogId,
          nextBeforeLogId,
          hasMoreBefore,
          bufferedLines: entries.length,
        };
      },
    };
  }

  window.ManzaraCore = {
    attachViewState,
    applyStopAllButton,
    api,
    applyPaginationControls,
    cssName,
    createTabController,
    createSseController,
    createRunLogViewer,
    DEFAULT_EVENT_TYPES: [...DEFAULT_EVENT_TYPES],
    VIEW_STATES: { ...VIEW_STATES },
    escapeHtml,
    formatEventBanner,
    formatDateTime,
    formatGlobalStatus,
    formatTime,
    isActiveStatus,
    renderLoadingTableRow,
    renderRunRowMessage,
    renderWorkflowFootnoteMessage,
    setStatusMessage,
  };
})();
