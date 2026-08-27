(() => {
  const RAIL_STORAGE_KEY = "manzara.ui.rail.expanded";
  const NAV_ITEMS = [
    { id: "tasks", title: "Tasks", href: "/tasks", icon: "list-checks" },
    { id: "database", title: "Database", href: "/database", icon: "database" },
    { id: "library", title: "Library", href: "/library", icon: "book-open" },
    { id: "gemini", title: "Gemini", href: "/gemini", icon: "key-round" },
  ];
  const shellState = {
    mounted: false,
    refreshCoordinator: null,
  };

  const LIBRARY_NAV_ITEMS = [
    { title: "Overview", href: "/library", match: /^\/library\/?$/ },
    { title: "Classifications", href: "/library/classifications", match: /^\/library\/classification/ },
    { title: "Personalities", href: "/library/personalities", match: /^\/library\/personalities/ },
    { title: "Publishers", href: "/library/publishers", match: /^\/library\/publishers/ },
    { title: "Collections", href: "/library/collections", match: /^\/library\/collections/ },
    { title: "Document cleanup", href: "/library/document-cleanup", match: /^\/library\/document-cleanup/ },
  ];

  function navMarkup(activePage) {
    return NAV_ITEMS.map((item) => {
      const active = item.id === activePage;
      return `
        <a href="${item.href}" class="rail-link ${active ? "active" : ""}"
           ${active ? 'aria-current="page"' : ""} title="${item.title}">
          <i data-lucide="${item.icon}"></i><span class="rail-label">${item.title}</span>
        </a>
      `;
    }).join("");
  }

  function shellMarkup(activePage) {
    return `
      <div class="app-shell">
        <aside class="side-rail" aria-label="Primary navigation">
          <div class="rail-top">
            <a class="rail-brand" href="/tasks" title="Manzara">
              <i data-lucide="sparkles"></i><span class="rail-label">Manzara</span>
            </a>
            <button id="rail-toggle" class="rail-toggle icon-btn quiet" type="button"
                    title="Expand navigation" aria-label="Expand navigation">
              <i data-lucide="panel-left"></i>
            </button>
          </div>
          <nav class="rail-nav">${navMarkup(activePage)}</nav>
        </aside>
        <div class="shell-main">
          <header class="topbar">
            <div class="top-controls">
              <span id="stream-state" class="stream-state" data-state="connecting">
                <span class="status-dot"></span><span>Connecting</span>
              </span>
              <div id="global-status" class="status-pill">Tasks: 0</div>
              <button id="stop-all-btn" class="icon-btn quiet" type="button"
                      title="Stop all running tasks" aria-label="Stop all tasks" disabled>
                <i data-lucide="square"></i>
              </button>
            </div>
          </header>
          <div id="page-slot"></div>
          <footer class="footer">
            <span>Manzara</span>
            <span id="last-event">Waiting for events</span>
          </footer>
        </div>
      </div>
    `;
  }

  function setRailExpanded(expanded, { persist = true } = {}) {
    const normalized = Boolean(expanded);
    document.documentElement.classList.toggle("rail-expanded", normalized);
    document.body.classList.toggle("rail-expanded", normalized);
    const button = document.getElementById("rail-toggle");
    if (button) {
      button.title = normalized ? "Collapse navigation" : "Expand navigation";
      button.setAttribute("aria-label", button.title);
      button.setAttribute("aria-expanded", normalized ? "true" : "false");
    }
    if (!persist) return;
    try {
      localStorage.setItem(RAIL_STORAGE_KEY, normalized ? "1" : "0");
    } catch (_error) {
      // Local storage is optional UI state.
    }
  }

  function readRailExpanded() {
    if (typeof window.ManzaraShellState?.initialExpanded === "boolean") {
      return window.ManzaraShellState.initialExpanded;
    }
    try {
      const stored = localStorage.getItem(RAIL_STORAGE_KEY);
      if (stored === "1") return true;
      if (stored === "0") return false;
    } catch (_error) {
      // Fall through to the viewport default.
    }
    return typeof matchMedia === "function"
      ? matchMedia("(min-width: 901px)").matches
      : true;
  }

  function attachShellHandlers() {
    document.getElementById("rail-toggle")?.addEventListener("click", () => {
      setRailExpanded(!document.documentElement.classList.contains("rail-expanded"));
    });
  }

  function setConnectionState(status) {
    const node = document.getElementById("stream-state");
    if (!node) return;
    const normalized = ["live", "reconnecting"].includes(String(status))
      ? String(status)
      : "connecting";
    node.dataset.state = normalized;
    const label = node.querySelector("span:last-child");
    if (label) {
      label.textContent = normalized === "live"
        ? "Live"
        : normalized === "reconnecting" ? "Reconnecting" : "Connecting";
    }
  }

  function renderSystemState(payload) {
    const global = payload?.global || {};
    const status = document.getElementById("global-status");
    if (status) {
      status.textContent = window.ManzaraCore.formatGlobalStatus(
        global.active_tasks,
      );
    }
    window.ManzaraCore.applyStopAllButton(
      document.getElementById("stop-all-btn"),
      global.stop_all_state,
    );
    window.lucide?.createIcons?.();
  }

  async function refreshSystemState() {
    const payload = await window.ManzaraCore.api("/api/system/state");
    renderSystemState(payload);
  }

  function queueSystemRefresh(delayMs = 80) {
    window.ManzaraCore.scheduleRefresh(shellState, refreshSystemState, delayMs);
  }

  function handleEvent(payload) {
    const lastEvent = document.getElementById("last-event");
    if (lastEvent) {
      lastEvent.textContent = window.ManzaraCore.formatEventBanner(payload);
    }
    const eventType = String(payload?.type || "");
    if (
      eventType === "task.started"
      || eventType === "task.completed"
      || eventType === "task.failed"
      || eventType === "task.stopped"
      || eventType === "system.stop_all_requested"
    ) {
      queueSystemRefresh();
    }
  }

  function createLibrarySubnav() {
    const nav = document.createElement("nav");
    nav.className = "context-nav";
    nav.setAttribute("aria-label", "Library sections");
    const pathname = window.location.pathname;
    LIBRARY_NAV_ITEMS.forEach((item) => {
      const link = document.createElement("a");
      link.href = item.href;
      link.textContent = item.title;
      if (item.match.test(pathname)) {
        link.classList.add("active");
        link.setAttribute("aria-current", "page");
      }
      nav.appendChild(link);
    });
    return nav;
  }

  function mount() {
    if (shellState.mounted) return;
    const pageContent = document.querySelector("[data-page-content]");
    if (!pageContent) return;
    const activePage = String(document.body.dataset.manzaraPage || "tasks");
    const host = document.createElement("div");
    host.innerHTML = shellMarkup(activePage);
    const shell = host.firstElementChild;
    document.body.insertBefore(shell, pageContent);
    const pageSlot = shell.querySelector("#page-slot");
    if (activePage === "library") {
      pageSlot.appendChild(createLibrarySubnav());
    }
    pageSlot.appendChild(pageContent);
    shellState.mounted = true;
    setRailExpanded(readRailExpanded(), { persist: false });
    attachShellHandlers();
    window.ManzaraUI?.ensureHosts?.();
    window.lucide?.createIcons?.();
    queueSystemRefresh(0);
  }

  window.ManzaraShell = {
    mount,
    handleEvent,
    setConnectionState,
  };

  mount();
})();
