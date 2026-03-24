(() => {
  function createPrompt() {
    const dialog = document.getElementById("sudo-dialog");
    const titleNode = document.getElementById("sudo-title");
    const messageNode = document.getElementById("sudo-message");
    const errorNode = document.getElementById("sudo-error");
    const inputNode = document.getElementById("sudo-password-input");
    const submitBtn = document.getElementById("sudo-submit-btn");
    const cancelBtn = document.getElementById("sudo-cancel-btn");

    let resolver = null;

    function resolve(value) {
      if (resolver) {
        const current = resolver;
        resolver = null;
        current(value);
      }
    }

    function closeWith(value) {
      resolve(value);
      if (dialog instanceof HTMLDialogElement && dialog.open) {
        dialog.close();
      }
    }

    function reset() {
      if (inputNode instanceof HTMLInputElement) {
        inputNode.value = "";
      }
      if (errorNode) {
        errorNode.textContent = "";
      }
    }

    if (cancelBtn) {
      cancelBtn.addEventListener("click", () => closeWith(null));
    }
    if (submitBtn) {
      submitBtn.addEventListener("click", (event) => {
        event.preventDefault();
        if (!(inputNode instanceof HTMLInputElement)) {
          closeWith(null);
          return;
        }
        const value = inputNode.value;
        if (!value) {
          if (errorNode) {
            errorNode.textContent = "Password is required.";
          }
          inputNode.focus();
          return;
        }
        closeWith(value);
      });
    }
    if (inputNode instanceof HTMLInputElement) {
      inputNode.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          submitBtn?.click();
        }
      });
    }
    if (dialog instanceof HTMLDialogElement) {
      dialog.addEventListener("close", () => {
        resolve(null);
        reset();
      });
    }

    async function request(params = {}) {
      const title = String(params.title || "Sudo Authentication");
      const message = String(
        params.message || "This action requires sudo privileges. Enter your password."
      );
      const error = String(params.error || "");

      if (!(dialog instanceof HTMLDialogElement) || !(inputNode instanceof HTMLInputElement)) {
        const fallback = window.prompt(message);
        return fallback && fallback.length > 0 ? fallback : null;
      }

      if (titleNode) {
        titleNode.textContent = title;
      }
      if (messageNode) {
        messageNode.textContent = message;
      }
      if (errorNode) {
        errorNode.textContent = error;
      }
      inputNode.value = "";

      if (!dialog.open) {
        dialog.showModal();
      }
      window.setTimeout(() => inputNode.focus(), 0);

      return new Promise((resolvePromise) => {
        resolver = resolvePromise;
      });
    }

    function teardown() {
      if (dialog instanceof HTMLDialogElement && dialog.open) {
        dialog.close();
      } else {
        resolve(null);
      }
    }

    return {
      request,
      teardown,
    };
  }

  async function runWithSudoPrompt({ execute, prompt, title, contextLabel }) {
    let sudoPassword = null;
    let errorText = "";
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const result = await execute(sudoPassword);
      const action = String(result?.action || "");
      if (action !== "sudo_password_required" && action !== "sudo_password_invalid") {
        return result;
      }

      if (!prompt || typeof prompt.request !== "function") {
        return result;
      }

      const baseMessage = String(result?.message || "Sudo password is required.");
      const password = await prompt.request({
        title: title || "Sudo Authentication",
        message: contextLabel ? `${contextLabel}. ${baseMessage}` : baseMessage,
        error: errorText,
      });
      if (!password) {
        return {
          action: "noop",
          reason: "sudo_prompt_cancelled",
          message: "Sudo prompt cancelled by user.",
        };
      }
      sudoPassword = password;
      errorText =
        action === "sudo_password_invalid" ? "Password is incorrect. Please try again." : "";
    }
    return {
      action: "noop",
      reason: "sudo_retry_exhausted",
      message: "Unable to authenticate sudo after multiple attempts.",
    };
  }

  window.ManzaraSudoPrompt = {
    createPrompt,
    runWithSudoPrompt,
  };
})();
