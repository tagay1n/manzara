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

  window.ManzaraCore = {
    applyStopAllButton,
    api,
    createSseController,
    DEFAULT_EVENT_TYPES: [...DEFAULT_EVENT_TYPES],
    formatEventBanner,
    formatDateTime,
    formatTime,
  };
})();
