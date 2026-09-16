(() => {
  const DEFAULT_EVENT_TYPES = [
    "task.started",
    "task.progress",
    "task.log",
    "task.stop_requested",
    "task.force_stop_requested",
    "task.stopped",
    "task.artifact",
    "task.completed",
    "task.failed",
    "conveyor.updated",
    "conveyor.started",
    "conveyor.stage_started",
    "conveyor.task_started",
    "conveyor.task_finished",
    "conveyor.stop_requested",
    "conveyor.stopped",
    "conveyor.completed",
    "conveyor.failed",
    "task.renamed",
    "flow.renamed",
    "gemini.key.used",
    "gemini.key.success",
    "gemini.key.error",
    "gemini.key.exhausted",
    "gemini.request.rejected",
    "gemini.key.reset",
    "gemini.all_reset",
    "gemini.pause.started",
    "gemini.pause.ended",
    "gemini.blackout.overridden",
    "library.collections.updated",
    "system.stop_all_requested",
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

  function formatGlobalStatus(activeTasks) {
    return `Tasks: ${Number(activeTasks || 0)}`;
  }

  function eventCursorFromSnapshot(payload) {
    const cursor = Number(payload?.event_cursor || 0);
    return Number.isFinite(cursor) && cursor > 0 ? Math.trunc(cursor) : 0;
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

  function toLucideIcon(name, fallback = "play") {
    const raw = String(name || "").trim();
    const fallbackName = String(fallback || "play").trim().toLowerCase() || "play";
    if (!raw) return fallbackName;
    const normalized = raw
      .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
      .replaceAll("_", "-")
      .replace(/\s+/g, "-")
      .toLowerCase()
      .replace(/[^a-z0-9-]+/g, "-")
      .replace(/-{2,}/g, "-")
      .replace(/^-+|-+$/g, "");
    return normalized || fallbackName;
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

  function createRefreshCoordinator(worker) {
    if (typeof worker !== "function") {
      throw new TypeError("refresh worker must be a function");
    }
    let running = false;
    let queued = false;
    let waiters = [];

    function settleWaiters(error = null) {
      const current = waiters;
      waiters = [];
      current.forEach(({ resolve, reject }) => {
        if (error) reject(error);
        else resolve();
      });
    }

    async function run() {
      running = true;
      try {
        do {
          queued = false;
          await worker();
        } while (queued);
        settleWaiters();
      } catch (error) {
        queued = false;
        settleWaiters(error);
      } finally {
        running = false;
      }
    }

    function request() {
      const completion = new Promise((resolve, reject) => {
        waiters.push({ resolve, reject });
      });
      if (running) {
        queued = true;
      } else {
        void run();
      }
      return completion;
    }

    return {
      request,
      getState() {
        return { running, queued };
      },
    };
  }

  function scheduleRefresh(state, worker, delayMs = 0) {
    if (!state || typeof state !== "object") {
      throw new TypeError("refresh state must be an object");
    }
    if (typeof worker !== "function") {
      throw new TypeError("refresh worker must be a function");
    }
    if (!state.refreshCoordinator) {
      state.refreshCoordinator = createRefreshCoordinator(worker);
    }
    if (state.refreshTimer) return;
    state.refreshTimer = setTimeout(() => {
      state.refreshTimer = null;
      state.refreshCoordinator.request().catch((error) => {
        console.error(error);
      });
    }, Math.max(0, Number(delayMs || 0)));
  }

  const RECONCILIATION_EVENTS = new Set([
    "task.started",
    "task.stop_requested",
    "task.force_stop_requested",
    "task.artifact",
    "task.completed",
    "task.failed",
    "task.stopped",
    "task.renamed",
    "flow.renamed",
    "library.collection_updated",
  ]);

  function eventNeedsReconciliation(payload) {
    const eventType = String(payload?.type || "");
    if (RECONCILIATION_EVENTS.has(eventType)) return true;
    return (
      eventType.startsWith("gemini.")
      || eventType.startsWith("library.")
    );
  }

  function taskStatusFromEvent(payload) {
    const eventType = String(payload?.type || "");
    const explicitStatus = String(payload?.payload?.status || "");
    if (explicitStatus) return explicitStatus;
    const statuses = {
      "task.started": "starting",
      "task.progress": "running",
      "task.stop_requested": "stopping_graceful",
      "task.force_stop_requested": "stopping_force",
      "task.completed": "completed",
      "task.failed": "failed",
      "task.stopped": "stopped",
    };
    return statuses[eventType] || "";
  }

  function applyTaskEventState(root, payload) {
    const eventType = String(payload?.type || "");
    const taskId = String(payload?.task_id || "");
    const runId = Number(payload?.run_id || 0);
    const status = taskStatusFromEvent(payload);
    if (!root || typeof root !== "object" || !taskId || !status) return false;
    let changed = false;
    const seen = new WeakSet();

    function visit(value) {
      if (!value || typeof value !== "object" || seen.has(value)) return;
      seen.add(value);
      if (String(value.task_id || "") === taskId && Object.hasOwn(value, "run")) {
        const eventProgress = payload?.payload?.progress;
        const currentStatus = String(value.run?.status || "");
        const nextStatus = (
          eventType === "task.progress"
          && (currentStatus === "stopping_graceful" || currentStatus === "stopping_force")
        ) ? currentStatus : status;
        value.run = {
          ...(value.run && typeof value.run === "object" ? value.run : {}),
          task_id: taskId,
          run_id: runId || value.run?.run_id || null,
          status: nextStatus,
          ...(eventProgress && typeof eventProgress === "object"
            ? { progress: { ...eventProgress } }
            : {}),
        };
        changed = true;
      }
      if (
        runId > 0
        && Number(value.run_id || 0) === runId
        && Object.hasOwn(value, "status")
      ) {
        const currentStatus = String(value.status || "");
        value.status = (
          eventType === "task.progress"
          && (currentStatus === "stopping_graceful" || currentStatus === "stopping_force")
        ) ? currentStatus : status;
        changed = true;
      }
      Object.values(value).forEach(visit);
    }

    visit(root);
    return changed;
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

  const TASK_STATUS_LABELS = {
    idle: "Idle",
    starting: "Starting",
    running: "Running",
    stopping_graceful: "Stopping",
    stopping_force: "Force stopping",
    completed: "Completed",
    failed: "Failed",
    stopped: "Stopped",
  };

  function taskStatusBadgeModel(run = {}, options = {}) {
    const status = String(run?.status || "idle");
    const active = isActiveStatus(status);
    const failed = status === "failed";
    const progress = run?.progress && typeof run.progress === "object" ? run.progress : {};
    const current = Number(progress.current);
    const total = Number(progress.total);
    const determinate = active
      && Number.isFinite(current)
      && current >= 0
      && Number.isFinite(total)
      && total > 0;
    const suppliedPercent = Number(progress.percent);
    const calculatedPercent = determinate ? (current / total) * 100 : 0;
    const percent = Math.round(Math.max(0, Math.min(
      100,
      Number.isFinite(suppliedPercent) ? suppliedPercent : calculatedPercent,
    )));
    return {
      status,
      statusClass: cssName(status, "idle"),
      label: String(options.label || TASK_STATUS_LABELS[status] || status),
      active,
      failed,
      determinate,
      current,
      total,
      percent,
    };
  }

  function renderTaskStatusBadge(run = {}, options = {}) {
    const model = taskStatusBadgeModel(run, options);
    const classes = [
      "task-status-badge",
      `task-status-${model.statusClass}`,
      model.active ? "is-active" : "",
      model.failed ? "is-failed" : "",
      model.determinate ? "has-progress" : "",
      options.compact ? "is-compact" : "",
    ].filter(Boolean).join(" ");
    const progressText = model.determinate
      ? `<span class="task-status-progress">${escapeHtml(model.current)} / ${escapeHtml(model.total)} · ${model.percent}%</span>`
      : "";
    const progressAttributes = model.determinate
      ? ` role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${model.percent}" aria-label="${escapeHtml(model.label)}: ${escapeHtml(model.current)} of ${escapeHtml(model.total)}, ${model.percent}%" style="--task-progress: ${model.percent}%"`
      : ` aria-label="${escapeHtml(model.label)}"`;
    return `<span class="${classes}"${progressAttributes}><span class="task-status-label">${escapeHtml(model.label)}</span>${progressText}</span>`;
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
      let normalizedMessage = "";
      if (text) {
        try {
          const parsed = JSON.parse(text);
          const detail = parsed?.detail;
          if (typeof detail === "string" && detail.trim()) {
            normalizedMessage = detail.trim();
          }
        } catch (_error) {
          // Non-JSON errors fall back to raw response text.
        }
      }
      throw new Error(normalizedMessage || text || `HTTP ${response.status}`);
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
    const initialCursor = Number(options.initialCursor || 0);

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
          window.ManzaraShell?.handleEvent?.(payload, event);
          onEvent(payload, event);
        });
      });

      next.onopen = () => {
        clearReconnectTimer();
        window.ManzaraShell?.setConnectionState?.("live");
        onOpen();
      };

      next.onerror = () => {
        window.ManzaraShell?.setConnectionState?.("reconnecting");
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
      const current = Number(getCursor() || 0);
      if (Number.isFinite(initialCursor) && initialCursor > current) {
        setCursor(initialCursor);
      }
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
    const onStateChange = typeof options.onStateChange === "function"
      ? options.onStateChange
      : null;

    let activeRunId = null;
    let nextAfterLogId = 0;
    let nextBeforeLogId = 0;
    let hasMoreBefore = false;
    let pollTimer = null;
    let loadingFollow = false;
    let loadingBackfill = false;
    let requestVersion = 0;
    let currentStatus = "closed";
    const entryIds = new Set();
    const entries = [];

    function emitState(status, error = null) {
      currentStatus = status;
      if (!onStateChange) return;
      onStateChange({
        status,
        activeRunId,
        bufferedLines: entries.length,
        hasMoreBefore,
        error: error ? String(error?.message || error) : "",
      });
    }

    function clearPollTimer() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function normalizedLineValue(raw) {
      return String(raw ?? "");
    }

    function parseWorkerLine(raw) {
      const line = normalizedLineValue(raw);
      const match = line.match(/^\[worker=([a-z0-9._-]+)\]\s?(.*)$/s);
      if (!match) return { workerId: "", message: line };
      return { workerId: match[1], message: match[2] };
    }

    function logDateParts(value) {
      const date = parseDate(value);
      if (!date) return { key: "unknown", dateLabel: "Date unavailable", timeLabel: "--:--:--" };
      const key = [date.getFullYear(), date.getMonth() + 1, date.getDate()].join("-");
      const dateLabel = new Intl.DateTimeFormat("en-GB", {
        day: "2-digit", month: "short", year: "numeric", timeZoneName: "short",
      }).format(date);
      const timeLabel = new Intl.DateTimeFormat("en-GB", {
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
      }).format(date);
      return { key, dateLabel, timeLabel };
    }

    const TERMINAL_WORKER_COLORS = [
      "#7dd3fc", // bright cyan
      "#fbbf24", // bright yellow
      "#86efac", // bright green
      "#f0abfc", // bright magenta
      "#fb7185", // bright red
      "#a5b4fc", // bright blue
      "#fdba74", // bright orange
      "#5eead4", // bright teal
    ];

    function workerColor(workerId) {
      const suffix = String(workerId || "").match(/-(\d+)$/);
      if (suffix) {
        return TERMINAL_WORKER_COLORS[(Number(suffix[1]) - 1) % TERMINAL_WORKER_COLORS.length];
      }
      let hash = 0;
      for (const character of String(workerId || "")) {
        hash = ((hash * 31) + character.charCodeAt(0)) >>> 0;
      }
      return TERMINAL_WORKER_COLORS[hash % TERMINAL_WORKER_COLORS.length];
    }

    function normalizeEntry(item, logId) {
      const parsed = parseWorkerLine(item?.line);
      return {
        log_id: logId,
        ts: String(item?.ts || ""),
        stream: String(item?.stream || "stdout"),
        workerId: parsed.workerId,
        message: parsed.message,
      };
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
      const ownerDocument = contentNode.ownerDocument;
      if (!ownerDocument?.createElement || !ownerDocument?.createDocumentFragment) {
        let previousDate = "";
        const textLines = [];
        for (const entry of entries) {
          const parts = logDateParts(entry.ts);
          if (parts.key !== previousDate) {
            textLines.push(`— ${parts.dateLabel} —`);
            previousDate = parts.key;
          }
          const worker = entry.workerId ? ` ${entry.workerId}` : "";
          textLines.push(`${parts.timeLabel}${worker} ${entry.message}`);
        }
        contentNode.textContent = `${textLines.join("\n")}\n`;
        return;
      }
      const fragment = ownerDocument.createDocumentFragment();
      let previousDate = "";
      for (const entry of entries) {
        const parts = logDateParts(entry.ts);
        if (parts.key !== previousDate) {
          const separator = ownerDocument.createElement("span");
          separator.className = "task-log-date-separator";
          separator.textContent = parts.dateLabel;
          fragment.append(separator, ownerDocument.createTextNode("\n"));
          previousDate = parts.key;
        }
        const row = ownerDocument.createElement("span");
        row.className = "task-log-row";
        row.dataset.worker = entry.workerId || "unassigned";
        if (entry.workerId && entry.workerId !== "coordinator") {
          row.style.setProperty("--worker-color", workerColor(entry.workerId));
        }
        const fullDateTime = formatDateTime(entry.ts, { includeSeconds: true, includeZone: true });
        const workerLabel = entry.workerId || "unassigned";
        row.title = `${fullDateTime} • ${workerLabel}`;
        row.setAttribute("aria-label", `${fullDateTime}, ${workerLabel}: ${entry.message}`);

        const time = ownerDocument.createElement("span");
        time.className = "task-log-time";
        time.textContent = parts.timeLabel;
        const worker = ownerDocument.createElement("span");
        worker.className = "task-log-worker";
        worker.textContent = workerLabel;
        const message = ownerDocument.createElement("span");
        message.className = "task-log-message";
        message.textContent = entry.message;
        row.append(time, worker, message);
        fragment.append(row, ownerDocument.createTextNode("\n"));
      }
      contentNode.replaceChildren(fragment);
    }

    function appendEntries(lines) {
      if (!Array.isArray(lines) || !lines.length || !contentNode) return 0;
      let appended = 0;
      for (const item of lines) {
        const logId = Number(item?.log_id || 0);
        if (!Number.isFinite(logId) || logId <= 0 || entryIds.has(logId)) continue;
        entryIds.add(logId);
        const entry = normalizeEntry(item, logId);
        entries.push(entry);
        appended += 1;
      }
      if (!appended) return 0;
      renderAll();
      return appended;
    }

    function prependEntries(lines) {
      if (!Array.isArray(lines) || !lines.length || !contentNode) return 0;
      const chunk = [];
      for (const item of lines) {
        const logId = Number(item?.log_id || 0);
        if (!Number.isFinite(logId) || logId <= 0 || entryIds.has(logId)) continue;
        entryIds.add(logId);
        chunk.push(normalizeEntry(item, logId));
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
      const version = requestVersion;
      const runId = activeRunId;
      loadingFollow = true;
      try {
        const wasNearBottom = isNearBottom();
        const payload = await fetchChunk({
          afterLogId: nextAfterLogId,
          limit: followLimit,
        });
        if (!payload || version !== requestVersion || runId !== activeRunId) return;
        appendEntries(payload.lines);
        applyCursorPayload(payload, { after: nextAfterLogId, before: nextBeforeLogId });
        if (wasNearBottom && contentNode) {
          contentNode.scrollTop = contentNode.scrollHeight;
        }
        emitState(entries.length ? "ready" : "empty");
      } catch (error) {
        if (version === requestVersion && runId === activeRunId) emitState("error", error);
        throw error;
      } finally {
        loadingFollow = false;
      }
    }

    async function loadOlder() {
      if (!activeRunId || loadingBackfill) return;
      if (!hasMoreBefore || nextBeforeLogId <= 0) return;
      const version = requestVersion;
      const runId = activeRunId;
      loadingBackfill = true;
      try {
        const previousHeight = contentNode ? contentNode.scrollHeight : 0;
        const previousTop = contentNode ? contentNode.scrollTop : 0;
        const payload = await fetchChunk({
          beforeLogId: nextBeforeLogId,
          limit: backfillLimit,
        });
        if (!payload || version !== requestVersion || runId !== activeRunId) return;
        prependEntries(payload.lines);
        applyCursorPayload(payload, { before: nextBeforeLogId, after: nextAfterLogId });
        if (contentNode) {
          const delta = contentNode.scrollHeight - previousHeight;
          contentNode.scrollTop = previousTop + delta;
        }
        emitState(entries.length ? "ready" : "empty");
      } catch (error) {
        if (version === requestVersion && runId === activeRunId) emitState("error", error);
        throw error;
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
      requestVersion += 1;
      const version = requestVersion;
      resetState();
      if (titleNode) {
        titleNode.textContent = `Logs • ${taskTitle} • run ${activeRunId}`;
      }
      if (dialogNode && !dialogNode.open && typeof dialogNode.showModal === "function") {
        dialogNode.showModal();
      }
      emitState("loading");

      try {
        const payload = await fetchChunk({ tail: true, limit: tailLimit });
        if (version !== requestVersion) return;
        if (payload) {
          appendEntries(payload.lines);
          applyCursorPayload(payload);
        }
        if (contentNode) {
          contentNode.scrollTop = contentNode.scrollHeight;
        }
        emitState(entries.length ? "ready" : "empty");

        pollTimer = setInterval(() => {
          pollFollow().catch((error) => console.error(error));
        }, Number.isFinite(pollIntervalMs) && pollIntervalMs > 0 ? pollIntervalMs : 1500);
      } catch (error) {
        if (version === requestVersion) emitState("error", error);
        throw error;
      }
    }

    function close(options = {}) {
      clearPollTimer();
      requestVersion += 1;
      activeRunId = null;
      if (options.keepContent !== true) {
        resetState();
      }
      if (options.closeDialog !== false && dialogNode && dialogNode.open) {
        dialogNode.close();
      }
      emitState("closed");
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
      getCopyText() {
        return entries.map((entry) => {
          const timestamp = formatDateTime(entry.ts, { includeSeconds: true, includeZone: true });
          const worker = entry.workerId || "unassigned";
          return `${timestamp} [${worker}] ${entry.message}`;
        }).join("\n");
      },
      getState() {
        return {
          activeRunId,
          nextAfterLogId,
          nextBeforeLogId,
          hasMoreBefore,
          bufferedLines: entries.length,
          status: currentStatus,
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
    toLucideIcon,
    createTabController,
    createSseController,
    createRunLogViewer,
    createRefreshCoordinator,
    scheduleRefresh,
    DEFAULT_EVENT_TYPES: [...DEFAULT_EVENT_TYPES],
    VIEW_STATES: { ...VIEW_STATES },
    escapeHtml,
    eventCursorFromSnapshot,
    formatEventBanner,
    formatDateTime,
    formatGlobalStatus,
    formatTime,
    eventNeedsReconciliation,
    applyTaskEventState,
    isActiveStatus,
    taskStatusBadgeModel,
    renderTaskStatusBadge,
    renderLoadingTableRow,
    renderRunRowMessage,
    renderWorkflowFootnoteMessage,
    setStatusMessage,
  };
})();
