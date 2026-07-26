(() => {
  let pendingDialogResolve = null;

  function ensureHosts() {
    if (!document.getElementById("toast-region")) {
      const region = document.createElement("div");
      region.id = "toast-region";
      region.className = "toast-region";
      region.setAttribute("aria-live", "polite");
      region.setAttribute("aria-atomic", "false");
      document.body.appendChild(region);
    }
    if (!document.getElementById("ui-dialog")) {
      const dialog = document.createElement("dialog");
      dialog.id = "ui-dialog";
      dialog.className = "ui-dialog";
      dialog.innerHTML = `
        <form method="dialog" class="ui-dialog-frame">
          <header class="ui-dialog-head">
            <div>
              <h2 id="ui-dialog-title">Confirm action</h2>
              <p id="ui-dialog-message"></p>
            </div>
            <button class="icon-btn quiet" value="cancel" aria-label="Close dialog" title="Close">
              <i data-lucide="x"></i>
            </button>
          </header>
          <div id="ui-dialog-input-wrap" class="ui-dialog-input-wrap" hidden>
            <label id="ui-dialog-input-label" for="ui-dialog-input">Value</label>
            <input id="ui-dialog-input" class="filter-input" autocomplete="off" />
          </div>
          <footer class="ui-dialog-actions">
            <button class="small-btn quiet" value="cancel">Cancel</button>
            <button id="ui-dialog-accept" class="small-btn primary" value="accept">Continue</button>
          </footer>
        </form>
      `;
      dialog.addEventListener("close", () => {
        if (!pendingDialogResolve) return;
        const resolve = pendingDialogResolve;
        pendingDialogResolve = null;
        resolve(dialog.returnValue === "accept");
      });
      document.body.appendChild(dialog);
    }
  }

  function toast(message, options = {}) {
    ensureHosts();
    const region = document.getElementById("toast-region");
    const item = document.createElement("div");
    const tone = ["success", "error", "warning"].includes(String(options.tone))
      ? String(options.tone)
      : "info";
    item.className = `toast toast-${tone}`;
    item.setAttribute("role", tone === "error" ? "alert" : "status");

    const text = document.createElement("span");
    text.textContent = String(message || "");
    item.appendChild(text);

    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss notification");
    close.textContent = "×";
    close.addEventListener("click", () => item.remove());
    item.appendChild(close);
    region.appendChild(item);

    const duration = options.duration === undefined
      ? (tone === "error" ? 0 : 4000)
      : Math.max(0, Number(options.duration || 0));
    if (duration > 0) {
      setTimeout(() => item.remove(), duration);
    }
    return item;
  }

  function openDialog(options = {}) {
    ensureHosts();
    const dialog = document.getElementById("ui-dialog");
    if (pendingDialogResolve) {
      pendingDialogResolve(false);
      pendingDialogResolve = null;
    }
    document.getElementById("ui-dialog-title").textContent =
      String(options.title || "Confirm action");
    document.getElementById("ui-dialog-message").textContent =
      String(options.message || "");
    document.getElementById("ui-dialog-accept").textContent =
      String(options.acceptLabel || "Continue");
    document.getElementById("ui-dialog-accept").className =
      `small-btn ${options.destructive ? "danger" : "primary"}`;

    const inputWrap = document.getElementById("ui-dialog-input-wrap");
    const input = document.getElementById("ui-dialog-input");
    const wantsInput = Boolean(options.input);
    inputWrap.hidden = !wantsInput;
    input.value = wantsInput ? String(options.value || "") : "";
    document.getElementById("ui-dialog-input-label").textContent =
      String(options.inputLabel || "Value");

    dialog.returnValue = "cancel";
    dialog.showModal();
    window.lucide?.createIcons?.();
    if (wantsInput) {
      requestAnimationFrame(() => {
        input.focus();
        input.select();
      });
    }

    return new Promise((resolve) => {
      pendingDialogResolve = (accepted) => {
        resolve({
          accepted,
          value: accepted && wantsInput ? input.value.trim() : null,
        });
      };
    });
  }

  async function confirmAction(options = {}) {
    const result = await openDialog(options);
    return result.accepted;
  }

  async function promptAction(options = {}) {
    const result = await openDialog({ ...options, input: true });
    return result.accepted ? result.value : null;
  }

  function setInlineMessage(node, message, options = {}) {
    if (!node) return;
    node.textContent = String(message || "");
    node.classList.toggle("is-error", Boolean(options.error));
    node.classList.toggle("is-success", Boolean(options.success));
  }

  async function runAction(button, action, options = {}) {
    if (typeof action !== "function") {
      throw new TypeError("action must be a function");
    }
    if (button?.dataset?.pending === "1") return null;
    const previousDisabled = Boolean(button?.disabled);
    if (button) {
      button.dataset.pending = "1";
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.classList.add("is-pending");
    }
    setInlineMessage(options.statusNode, options.pendingMessage || "", {});
    try {
      const result = await action();
      if (options.successMessage) {
        toast(options.successMessage, { tone: "success" });
        setInlineMessage(options.statusNode, options.successMessage, { success: true });
      }
      return result;
    } catch (error) {
      const message = String(error?.message || error || "Action failed");
      toast(message, { tone: "error" });
      setInlineMessage(options.statusNode, message, { error: true });
      throw error;
    } finally {
      if (button) {
        delete button.dataset.pending;
        button.disabled = previousDisabled;
        button.removeAttribute("aria-busy");
        button.classList.remove("is-pending");
      }
    }
  }

  function reportTaskActionResult(result) {
    if (!result || typeof result !== "object") return;
    const reason = String(result.reason || "");
    if ((result.action === "noop" || reason.startsWith("sudo_")) && result.message) {
      toast(String(result.message), { tone: reason ? "error" : "info" });
    }
  }

  window.ManzaraUI = {
    confirm: confirmAction,
    ensureHosts,
    prompt: promptAction,
    reportTaskActionResult,
    runAction,
    setInlineMessage,
    toast,
  };
})();
