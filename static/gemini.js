const state = {
  payload: null,
  viewState: "loading",
  refreshTimer: null,
  eventCursor: 0,
  eventStreamController: null,
};

const viewState = window.ManzaraCore.attachViewState(state, "loading");

async function api(path, options = {}) {
  return window.ManzaraCore.api(path, options);
}

function escapeHtml(value) {
  return window.ManzaraCore.escapeHtml(value);
}

function formatDateTime(value) {
  return window.ManzaraCore.formatDateTime(value);
}

function renderStats(summary, globalState) {
  const statGrid = document.getElementById("gemini-stat-grid");
  if (!statGrid) return;
  const pauseLabel = globalState.pause_active
    ? `Paused until ${formatDateTime(globalState.pause_until)}`
    : "No global pause";
  const blackoutLabel = globalState.blackout_active
    ? `Blackout active until ${formatDateTime(globalState.blackout_end_utc)}`
    : `Next blackout starts ${formatDateTime(globalState.blackout_start_utc)}`;

  statGrid.innerHTML = `
    <div class="stat-cell"><span>Accounts</span><strong>${Number(summary.accounts || 0)}</strong></div>
    <div class="stat-cell"><span>Keys</span><strong>${Number(summary.keys || 0)}</strong></div>
    <div class="stat-cell"><span>Models seen</span><strong>${Number(summary.models_seen || 0)}</strong></div>
    <div class="stat-cell"><span>Exhausted rows</span><strong>${Number(summary.exhausted_rows || 0)}</strong></div>
    <div class="stat-cell"><span>Pause</span><strong>${escapeHtml(pauseLabel)}</strong></div>
    <div class="stat-cell"><span>Blackout</span><strong>${escapeHtml(blackoutLabel)}</strong></div>
  `;
}

function renderAccounts(accounts) {
  const host = document.getElementById("gemini-accounts");
  if (!host) return;
  if (!accounts.length) {
    host.innerHTML = '<div class="run-row">No Gemini keys configured.</div>';
    return;
  }

  host.innerHTML = accounts
    .map((account) => {
      const keyRows = (account.keys || [])
        .map((key) => {
          const models = (key.models || [])
            .map((model) => {
              const exhausted = model.exhausted ? "yes" : "no";
              const cls = model.exhausted ? "panel-pill state-attention" : "panel-pill state-healthy";
              const lastError = model.last_error_text
                ? `<div class="gemini-model-error">${escapeHtml(String(model.last_error_text))}</div>`
                : "";
              return `
                <div class="gemini-model-card">
                  <div class="gemini-model-head">
                    <span class="gemini-model-name">${escapeHtml(model.model_name)}</span>
                    <span class="${cls}">${exhausted}</span>
                  </div>
                  <div class="gemini-model-meta">cycle ${Number(model.success_cycle || 0)}/${Number(model.attempts_cycle || 0)}</div>
                  <div class="gemini-model-meta">cooldown: ${escapeHtml(formatDateTime(model.cooldown_until))}</div>
                  <div class="gemini-model-meta">last used: ${escapeHtml(formatDateTime(model.last_used_at))}</div>
                  ${lastError}
                </div>
              `;
            })
            .join("");
          const exhaustedBadge = (key.exhausted_models || []).length
            ? `<span class="panel-pill state-attention">${Number((key.exhausted_models || []).length)} exhausted</span>`
            : '<span class="panel-pill state-healthy">healthy</span>';
          return `
            <div class="gemini-key-row">
              <div class="gemini-key-head">
                <div>
                  <div class="gemini-key-mask">${escapeHtml(key.masked_key)}</div>
                  <div class="gemini-key-id">${escapeHtml(key.key_id)}</div>
                </div>
                <div class="gemini-key-actions">
                  ${exhaustedBadge}
                  <button class="small-btn gemini-reset-key" data-key-id="${escapeHtml(key.key_id)}">Reset key</button>
                </div>
              </div>
              <div class="gemini-model-grid">
                ${models || '<div class="run-row">No model activity yet.</div>'}
              </div>
            </div>
          `;
        })
        .join("");

      return `
        <section class="gemini-account-card">
          <div class="gemini-account-head">
            <h3>${escapeHtml(account.account_id)}</h3>
            <span class="panel-pill state-running">${Number(account.key_count || 0)} keys</span>
          </div>
          ${keyRows}
        </section>
      `;
    })
    .join("");
}

function renderPayload(payload) {
  state.payload = payload;
  const gemini = payload?.gemini || {};
  const summary = gemini.summary || {};
  const globalState = gemini.global || {};
  const statusNode = document.getElementById("gemini-status");
  window.ManzaraCore.setStatusMessage(
    statusNode,
    `Cycle ${String(globalState.cycle_label || "-")} • Reset ${formatDateTime(globalState.reset_at_utc)}`,
  );
  document.getElementById("global-status").textContent =
    `Keys: ${Number(summary.keys || 0)} • Accounts: ${Number(summary.accounts || 0)}`;
  renderStats(summary, globalState);
  renderAccounts(gemini.accounts || []);
  viewState.set((gemini.accounts || []).length ? "ready" : "empty");
  if (window.lucide?.createIcons) {
    window.lucide.createIcons();
  }
}

function renderError(error) {
  viewState.set("error");
  window.ManzaraCore.setStatusMessage(
    document.getElementById("gemini-status"),
    `Gemini state unavailable: ${error?.message || String(error)}`,
    { error: true },
  );
  document.getElementById("gemini-accounts").innerHTML = window.ManzaraCore.renderRunRowMessage(
    `Gemini state unavailable: ${error?.message || String(error)}`,
    { error: true },
  );
}

async function refreshGemini() {
  try {
    const payload = await api("/api/gemini/state");
    renderPayload(payload);
  } catch (error) {
    renderError(error);
    throw error;
  }
}

function queueRefresh(delayMs = 150) {
  window.ManzaraCore.scheduleRefresh(state, refreshGemini, delayMs);
}

async function resetKey(keyId) {
  await api("/api/gemini/reset-key", {
    method: "POST",
    body: JSON.stringify({ key_id: keyId }),
  });
  queueRefresh(0);
}

async function resetAll() {
  const confirmed = await window.ManzaraUI.confirm({
    title: "Reset all exhausted keys",
    message: "Clear exhausted markers for every Gemini key and model?",
    acceptLabel: "Reset keys",
  });
  if (!confirmed) return;
  await api("/api/gemini/reset-all", {
    method: "POST",
    body: JSON.stringify({}),
  });
  queueRefresh(0);
}

function setupEventStream() {
  state.eventStreamController?.stop();
  state.eventStreamController = window.ManzaraCore.createSseController({
    eventTypes: window.ManzaraCore.DEFAULT_EVENT_TYPES,
    getCursor: () => Number(state.eventCursor || 0),
    setCursor: (nextCursor) => {
      state.eventCursor = Number(nextCursor || 0);
    },
    onEvent: (payload) => {
      document.getElementById("last-event").textContent = window.ManzaraCore.formatEventBanner(payload);
      const eventType = String(payload?.type || "");
      if (eventType.startsWith("gemini.")) {
        queueRefresh(0);
      }
    },
  });
  state.eventStreamController.start();
}

function attachUiHandlers() {
  document.getElementById("reset-all-btn").addEventListener("click", () => {
    resetAll().catch((error) => console.error(error));
  });

  document.getElementById("gemini-accounts").addEventListener("click", (event) => {
    const button = event.target.closest(".gemini-reset-key");
    if (!button) return;
    const keyId = String(button.dataset.keyId || "").trim();
    if (!keyId) return;
    resetKey(keyId).catch((error) => console.error(error));
  });
}

async function bootstrap() {
  attachUiHandlers();
  await refreshGemini();
  setupEventStream();
  window.addEventListener("beforeunload", () => {
    state.eventStreamController?.stop();
    state.eventStreamController = null;
  });
}

bootstrap().catch((error) => {
  console.error(error);
});
