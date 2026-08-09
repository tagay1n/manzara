(() => {
  const RAIL_STORAGE_KEY = "manzara.ui.rail.expanded";
  const NAV_ITEMS = [
    { id: "schedules", title: "Schedules", href: "/schedules", icon: "calendar-range" },
    { id: "tasks", title: "Tasks", href: "/tasks", icon: "list-checks" },
    { id: "database", title: "Database", href: "/database", icon: "database" },
    { id: "library", title: "Library", href: "/library", icon: "book-open" },
    { id: "gemini", title: "Gemini", href: "/gemini", icon: "key-round" },
  ];
  const STATIC_COMMANDS = [
    ...NAV_ITEMS.map((item) => ({ ...item, kind: "Page" })),
    { title: "Classifications", href: "/library/classifications", icon: "tags", kind: "Library" },
    { title: "Personalities", href: "/library/personalities", icon: "users", kind: "Library" },
    { title: "Publishers", href: "/library/publishers", icon: "building-2", kind: "Library" },
    { title: "Collections", href: "/library/collections", icon: "library", kind: "Library" },
    { title: "Document cleanup", href: "/library/document-cleanup", icon: "list-filter", kind: "Library" },
  ];

  const shellState = {
    mounted: false,
    commands: [...STATIC_COMMANDS],
    filteredCommands: [],
    selectedCommand: 0,
    commandLoaded: false,
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
            <button id="command-trigger" class="command-trigger" type="button"
                    aria-haspopup="dialog" aria-controls="command-dialog">
              <i data-lucide="search"></i>
              <span>Jump to page, task, flow…</span>
              <kbd>Ctrl K</kbd>
            </button>
            <div class="top-controls">
              <span id="stream-state" class="stream-state" data-state="connecting">
                <span class="status-dot"></span><span>Connecting</span>
              </span>
              <div id="global-status" class="status-pill">Tasks: 0 · Flows: 0</div>
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
      <dialog id="command-dialog" class="command-dialog">
        <div class="command-frame">
          <label class="command-input-wrap" for="command-input">
            <i data-lucide="search"></i>
            <input id="command-input" autocomplete="off" placeholder="Search pages, flows, and tasks" />
            <kbd>Esc</kbd>
          </label>
          <div id="command-results" class="command-results" role="listbox"></div>
          <div class="command-help">↑↓ navigate · Enter open · Esc close</div>
        </div>
      </dialog>
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

  function commandMatches(command, query) {
    if (!query) return true;
    const haystack = `${command.title || ""} ${command.kind || ""} ${command.subtitle || ""}`.toLowerCase();
    return haystack.includes(query.toLowerCase());
  }

  function renderCommands(query = "") {
    const host = document.getElementById("command-results");
    if (!host) return;
    shellState.filteredCommands = shellState.commands.filter((item) => commandMatches(item, query));
    shellState.selectedCommand = Math.min(
      Math.max(0, shellState.selectedCommand),
      Math.max(0, shellState.filteredCommands.length - 1),
    );
    host.replaceChildren();
    if (!shellState.filteredCommands.length) {
      const empty = document.createElement("div");
      empty.className = "command-empty";
      empty.textContent = "No matching destination";
      host.appendChild(empty);
      return;
    }
    shellState.filteredCommands.forEach((command, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `command-result ${index === shellState.selectedCommand ? "selected" : ""}`;
      button.dataset.commandIndex = String(index);
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", index === shellState.selectedCommand ? "true" : "false");
      const icon = document.createElement("i");
      icon.setAttribute("data-lucide", String(command.icon || "corner-down-right"));
      const copy = document.createElement("span");
      copy.className = "command-result-copy";
      const title = document.createElement("strong");
      title.textContent = String(command.title || "");
      const meta = document.createElement("small");
      meta.textContent = String(command.kind || "Page");
      copy.append(title, meta);
      button.append(icon, copy);
      button.addEventListener("click", () => {
        window.location.assign(command.href);
      });
      host.appendChild(button);
    });
    window.lucide?.createIcons?.();
    host.querySelector(".command-result.selected")?.scrollIntoView?.({ block: "nearest" });
  }

  async function loadDynamicCommands() {
    if (shellState.commandLoaded) return;
    shellState.commandLoaded = true;
    try {
      const payload = await window.ManzaraCore.api("/api/tasks");
      const dynamic = [];
      (payload.flows || []).forEach((flow) => {
        dynamic.push({
          title: String(flow.title || flow.panel_id),
          subtitle: String(flow.panel_id || ""),
          kind: "Flow",
          icon: "workflow",
          href: `/flows/${encodeURIComponent(flow.slug || flow.panel_id)}`,
        });
        (flow.tasks || []).forEach((task) => {
          dynamic.push({
            title: String(task.title || task.task_id),
            subtitle: String(flow.title || ""),
            kind: "Task",
            icon: window.ManzaraCore.toLucideIcon(task.icon_idle, "play"),
            href: `/tasks/${encodeURIComponent(task.slug || task.task_id)}`,
          });
        });
      });
      shellState.commands = [...STATIC_COMMANDS, ...dynamic];
    } catch (error) {
      window.ManzaraUI?.toast?.(`Navigation index unavailable: ${error.message || error}`, {
        tone: "error",
      });
    }
  }

  async function openCommands() {
    const dialog = document.getElementById("command-dialog");
    const input = document.getElementById("command-input");
    if (!dialog || !input) return;
    await loadDynamicCommands();
    shellState.selectedCommand = 0;
    input.value = "";
    renderCommands("");
    if (!dialog.open) dialog.showModal();
    input.focus();
  }

  function closeCommands() {
    const dialog = document.getElementById("command-dialog");
    if (dialog?.open) dialog.close();
  }

  function moveCommandSelection(delta) {
    if (!shellState.filteredCommands.length) return;
    shellState.selectedCommand =
      (shellState.selectedCommand + delta + shellState.filteredCommands.length)
      % shellState.filteredCommands.length;
    renderCommands(document.getElementById("command-input")?.value || "");
  }

  function attachShellHandlers() {
    document.getElementById("rail-toggle")?.addEventListener("click", () => {
      setRailExpanded(!document.documentElement.classList.contains("rail-expanded"));
    });
    document.getElementById("command-trigger")?.addEventListener("click", () => {
      void openCommands();
    });
    document.getElementById("command-input")?.addEventListener("input", (event) => {
      shellState.selectedCommand = 0;
      renderCommands(event.target.value);
    });
    document.getElementById("command-input")?.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        moveCommandSelection(1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        moveCommandSelection(-1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        const command = shellState.filteredCommands[shellState.selectedCommand];
        if (command) window.location.assign(command.href);
      }
    });
    document.getElementById("command-dialog")?.addEventListener("click", (event) => {
      if (event.target === event.currentTarget) closeCommands();
    });
    document.addEventListener("keydown", (event) => {
      const target = event.target;
      const typing = target instanceof HTMLInputElement
        || target instanceof HTMLTextAreaElement
        || target?.isContentEditable;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        void openCommands();
      } else if (event.key === "/" && !typing) {
        event.preventDefault();
        void openCommands();
      }
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
        global.active_workflows,
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
      || eventType.startsWith("workflow.")
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
    const dialog = host.lastElementChild;
    document.body.insertBefore(shell, pageContent);
    const pageSlot = shell.querySelector("#page-slot");
    if (activePage === "library") {
      pageSlot.appendChild(createLibrarySubnav());
    }
    pageSlot.appendChild(pageContent);
    document.body.appendChild(dialog);
    shellState.mounted = true;
    setRailExpanded(readRailExpanded(), { persist: false });
    attachShellHandlers();
    window.ManzaraUI?.ensureHosts?.();
    window.lucide?.createIcons?.();
    queueSystemRefresh(0);
  }

  window.ManzaraShell = {
    mount,
    openCommands,
    handleEvent,
    setConnectionState,
  };

  mount();
})();
