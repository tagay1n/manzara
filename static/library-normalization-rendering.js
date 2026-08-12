"use strict";

(function attachNormalizationRendering(global) {
  function create({ state, escapeHtml, formatDateTime, encodeKey }) {
    function renderSummary() {
      const payload = state.summaryPayload || {};
      const dashboard = payload.dashboard || {};
      const stats = dashboard.stats || {};
      const suggestions = dashboard.suggestions || {};
      const statusNode = document.getElementById("normalization-status");

      if (dashboard.available) {
        const source = dashboard.config_source ? ` • source: ${dashboard.config_source}` : "";
        statusNode.textContent = `${state.pageTitle} loaded${source}`;
        statusNode.classList.remove("library-status-error");
      } else {
        statusNode.textContent = `Normalization unavailable: ${dashboard.error || "unknown error"}`;
        statusNode.classList.add("library-status-error");
      }

      document.getElementById("normalization-stat-grid").innerHTML = `
        <div class="library-stat-card"><span class="library-stat-label">Total Aliases</span><span class="library-stat-value">${stats.total_aliases || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Docs With Entities</span><span class="library-stat-value">${stats.docs_with_entities || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Canonicals</span><span class="library-stat-value">${stats.canonicals || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Linked</span><span class="library-stat-value">${stats.linked || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Unreviewed</span><span class="library-stat-value">${stats.unreviewed || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Suggested</span><span class="library-stat-value">${stats.suggested || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Coverage</span><span class="library-stat-value">${Number(stats.coverage_pct || 0).toFixed(2)}%</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Open Suggestions</span><span class="library-stat-value">${suggestions.open_total || 0}</span></div>
      `;
    }

    function canonicalOptionsHtml(selectedCanonicalId = null, includeBlank = true) {
      const options = [];
      if (includeBlank) {
        options.push('<option value="">Select canonical...</option>');
      }
      for (const canonical of state.canonicals) {
        const canonicalId = Number(canonical.canonical_id || 0);
        const selected = selectedCanonicalId && Number(selectedCanonicalId) === canonicalId ? " selected" : "";
        options.push(
          `<option value="${canonicalId}"${selected}>${canonicalId} • ${escapeHtml(
            canonical.display_name || ""
          )}</option>`
        );
      }
      return options.join("");
    }

    function renderCanonicals() {
      const payload = state.canonicalsPayload || {};
      const statusNode = document.getElementById("canonical-status");
      if (!payload.available) {
        statusNode.textContent = `Canonical registry unavailable: ${payload.error || "unknown error"}`;
        statusNode.classList.add("library-status-error");
        document.getElementById("canonical-table-body").innerHTML = "";
        return;
      }
      statusNode.classList.remove("library-status-error");
      statusNode.textContent = `Loaded ${payload.items.length} canonical entries`;
      state.canonicals = payload.items || [];

      document.getElementById("canonical-table-body").innerHTML = (payload.items || [])
        .map(
          (item) => `
          <tr>
            <td>${item.canonical_id || "-"}</td>
            <td>${escapeHtml(item.display_name || "-")}</td>
            <td>${escapeHtml(item.normalized_name || "-")}</td>
            <td>${item.linked_aliases || 0}</td>
            <td>${escapeHtml(item.status || "active")}</td>
            <td>${escapeHtml(item.notes || "-")}</td>
          </tr>
        `
        )
        .join("");

      document.getElementById("tab-badge-canonicals").textContent = String((payload.items || []).length);
      document.getElementById("queue-bulk-canonical").innerHTML = canonicalOptionsHtml(null, true);
    }

    function queueFilters() {
      return {
        search: document.getElementById("queue-filter-search").value.trim(),
        status: document.getElementById("queue-filter-status").value,
        scriptLabel: document.getElementById("queue-filter-script").value,
        minDocs: Number(document.getElementById("queue-filter-min-docs").value || "0"),
      };
    }

    function queueUrl() {
      const filters = queueFilters();
      const params = new URLSearchParams({
        status: filters.status,
        search: filters.search,
        script_label: filters.scriptLabel,
        min_docs: String(Math.max(0, filters.minDocs || 0)),
        page: String(state.queuePage),
        page_size: String(state.queuePageSize),
      });
      return `/api/library/normalization/${encodeURIComponent(state.entityType)}/queue?${params.toString()}`;
    }

    function queueSelectedRawNames() {
      return Array.from(document.querySelectorAll(".queue-row-select:checked")).map((el) =>
        decodeKey(el.dataset.raw || "")
      );
    }

    function clearQueueSelection() {
      document.querySelectorAll(".queue-row-select").forEach((node) => {
        node.checked = false;
      });
      document.getElementById("queue-select-all").checked = false;
    }

    function renderQueue() {
      const payload = state.queuePayload || {};
      const statusNode = document.getElementById("queue-status");
      if (!payload.available) {
        statusNode.textContent = `Queue unavailable: ${payload.error || "unknown error"}`;
        statusNode.classList.add("library-status-error");
        document.getElementById("queue-table-body").innerHTML = "";
        return;
      }
      statusNode.classList.remove("library-status-error");
      statusNode.textContent = `Loaded ${payload.items.length} rows from ${payload.total} total`;
      state.queueRows = payload.items || [];

      const tableBody = document.getElementById("queue-table-body");
      tableBody.innerHTML = (payload.items || [])
        .map((row) => {
          const key = encodeKey(row.raw_name || "");
          const suggestion = row.suggestion || null;
          const canonicalHint = row.canonical_name || (suggestion?.target_canonical_id ? `#${suggestion.target_canonical_id}` : "-");
          const suggestionText = suggestion
            ? `${suggestion.kind || "-"} • ${(Number(suggestion.confidence || 0) * 100).toFixed(0)}%`
            : "-";
          return `
          <tr>
            <td><input class="queue-row-select" type="checkbox" data-raw="${key}" /></td>
            <td>${escapeHtml(row.raw_name || "-")}<div class="workflow-footnote">${escapeHtml(row.normalized_name || "-")}</div></td>
            <td>${escapeHtml(row.script_label || "other")}</td>
            <td>${row.docs_count || 0}</td>
            <td>${row.mentions_count || 0}</td>
            <td>${escapeHtml(row.queue_status || "-")}</td>
            <td>
              <div>${escapeHtml(canonicalHint)}</div>
              <select class="filter-select queue-link-select" data-raw="${key}">
                ${canonicalOptionsHtml(row.canonical_id || suggestion?.target_canonical_id || null, true)}
              </select>
            </td>
            <td>${escapeHtml(suggestionText)}</td>
            <td>
              <div class="normalization-row-actions">
                <button class="small-btn queue-action-btn" data-action="link" data-raw="${key}">Link</button>
                <button class="small-btn queue-action-btn" data-action="create" data-raw="${key}">Create</button>
                <button class="small-btn queue-action-btn" data-action="reject" data-raw="${key}">Reject</button>
                <button class="small-btn queue-action-btn" data-action="evidence" data-raw="${key}">Evidence</button>
              </div>
            </td>
          </tr>
        `;
        })
        .join("");

      document.getElementById("queue-page-label").textContent = `Page ${payload.page} / ${payload.total_pages}`;
      document.getElementById("queue-page-prev").disabled = payload.page <= 1;
      document.getElementById("queue-page-next").disabled = payload.page >= payload.total_pages;
      clearQueueSelection();
    }

    function renderSuggestions() {
      const payload = state.suggestionsPayload || {};
      const statusNode = document.getElementById("suggestions-status");
      if (!payload.available) {
        statusNode.textContent = `Suggestions unavailable: ${payload.error || "unknown error"}`;
        statusNode.classList.add("library-status-error");
        document.getElementById("suggestions-table-body").innerHTML = "";
        return;
      }
      statusNode.classList.remove("library-status-error");
      statusNode.textContent = `Loaded ${payload.items.length} open suggestions`;
      document.getElementById("tab-badge-suggestions").textContent = String((payload.items || []).length);
      document.getElementById("suggestions-table-body").innerHTML = (payload.items || [])
        .map((row) => {
          const key = encodeKey(row.raw_name || "");
          const targetLabel = row.target_canonical_name
            ? `${row.target_canonical_id} • ${row.target_canonical_name}`
            : row.target_canonical_id
              ? `#${row.target_canonical_id}`
              : "-";
          return `
          <tr>
            <td>${escapeHtml(row.raw_name || "-")}</td>
            <td>${escapeHtml(row.suggestion_kind || "-")}</td>
            <td>${escapeHtml(targetLabel)}</td>
            <td>${(Number(row.confidence || 0) * 100).toFixed(1)}% • ${escapeHtml(row.confidence_band || "-")}</td>
            <td>${escapeHtml(row.rationale || "-")}</td>
            <td>
              <div class="normalization-row-actions">
                <button class="small-btn suggestion-action-btn" data-action="accept" data-raw="${key}" data-suggestion-id="${row.suggestion_id || 0}">Accept</button>
                <button class="small-btn suggestion-action-btn" data-action="reject" data-raw="${key}" data-suggestion-id="${row.suggestion_id || 0}">Reject</button>
                <button class="small-btn suggestion-action-btn" data-action="queue" data-raw="${key}">Open</button>
              </div>
            </td>
          </tr>
        `;
        })
        .join("");
    }

    function renderMergeCandidates() {
      const payload = state.mergePayload || {};
      const statusNode = document.getElementById("merge-status");
      if (!payload.available) {
        statusNode.textContent = `Merge candidates unavailable: ${payload.error || "unknown error"}`;
        statusNode.classList.add("library-status-error");
        document.getElementById("merge-root").innerHTML = "";
        return;
      }
      statusNode.classList.remove("library-status-error");
      statusNode.textContent = `Found ${payload.summary?.candidate_count || 0} merge candidates`;
      document.getElementById("merge-root").innerHTML = (payload.items || [])
        .map((item) => {
          const left = item.left || {};
          const right = item.right || {};
          const recommended = Number(item.recommended_primary_canonical_id || 0);
          const source = recommended === Number(left.canonical_id || 0) ? right : left;
          const target = recommended === Number(left.canonical_id || 0) ? left : right;
          return `
          <div class="duplicate-card">
            <div class="duplicate-head">
              <span class="duplicate-path">${escapeHtml(left.display_name || "-")} ↔ ${escapeHtml(right.display_name || "-")}</span>
              <span class="panel-pill">score ${Number(item.score || 0).toFixed(3)}</span>
            </div>
            <div class="workflow-footnote">
              Recommended: merge #${source.canonical_id || "-"} into #${target.canonical_id || "-"} • impact ${item.impact || 0}
            </div>
            <div class="normalization-row-actions">
              <button
                class="small-btn merge-apply-btn"
                data-source-id="${source.canonical_id || 0}"
                data-target-id="${target.canonical_id || 0}"
              >
                Merge Now
              </button>
            </div>
          </div>
        `;
        })
        .join("");
    }

    function renderQuality() {
      const quality = state.qualityPayload || {};
      const statusNode = document.getElementById("quality-status");
      if (!quality.available) {
        statusNode.textContent = `Quality unavailable: ${quality.error || "unknown error"}`;
        statusNode.classList.add("library-status-error");
        document.getElementById("quality-stat-grid").innerHTML = "";
        document.getElementById("quality-unresolved").innerHTML = "";
        return;
      }
      statusNode.classList.remove("library-status-error");
      statusNode.textContent = "Quality metrics loaded";
      const stats = quality.stats || {};
      document.getElementById("quality-stat-grid").innerHTML = `
        <div class="library-stat-card"><span class="library-stat-label">Total Aliases</span><span class="library-stat-value">${stats.total_aliases || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Linked</span><span class="library-stat-value">${stats.linked_aliases || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Rejected</span><span class="library-stat-value">${stats.rejected_aliases || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Unresolved</span><span class="library-stat-value">${stats.unresolved_aliases || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Unresolved Docs</span><span class="library-stat-value">${stats.unresolved_docs_estimate || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Duplicate Keys</span><span class="library-stat-value">${stats.duplicate_normalized_keys || 0}</span></div>
        <div class="library-stat-card"><span class="library-stat-label">Coverage</span><span class="library-stat-value">${Number(stats.coverage_pct || 0).toFixed(2)}%</span></div>
      `;

      const unresolved = (state.summaryPayload?.dashboard?.top_unresolved || []).slice(0, 16);
      document.getElementById("quality-unresolved").innerHTML =
        unresolved.length === 0
          ? '<div class="run-row">No unresolved aliases.</div>'
          : unresolved
              .map(
                (item) => `
              <div class="library-top-row">
                <div class="library-top-main">
                  <span class="library-top-ddc">${escapeHtml(item.raw_name || "-")}</span>
                  <span class="library-top-path">${escapeHtml(item.normalized_name || "-")} • ${escapeHtml(item.script_label || "other")}</span>
                </div>
                <span class="library-top-count">${item.docs_count || 0}</span>
              </div>
            `
              )
              .join("");
    }

    function historyItemDescription(item) {
      const action = String(item.action || "");
      const payload = item.payload || {};
      if (action === "link_alias") {
        return `link ${payload.raw_name || "-"} -> #${payload.canonical_id || "-"}`;
      }
      if (action === "reject_alias") {
        return `reject ${payload.raw_name || "-"}`;
      }
      if (action === "create_and_link_alias") {
        return `create+link ${payload.raw_name || "-"} -> #${payload.created_canonical_id || "-"}`;
      }
      if (action === "merge_canonicals") {
        const source = payload.source_before?.canonical_id || "-";
        const target = payload.target_before?.canonical_id || "-";
        return `merge #${source} -> #${target}`;
      }
      if (action === "refresh_suggestions") {
        return `refresh suggestions (${payload.generated || 0})`;
      }
      if (action === "bulk_link_aliases") {
        return `bulk link ${((payload.raw_names || []).length || 0)} aliases`;
      }
      if (action === "bulk_reject_aliases") {
        return `bulk reject ${((payload.raw_names || []).length || 0)} aliases`;
      }
      return action || "event";
    }

    function renderHistory() {
      const payload = state.historyPayload || {};
      const statusNode = document.getElementById("history-status");
      if (!payload.available) {
        statusNode.textContent = `History unavailable: ${payload.error || "unknown error"}`;
        statusNode.classList.add("library-status-error");
        document.getElementById("history-root").innerHTML = "";
        return;
      }
      statusNode.classList.remove("library-status-error");
      statusNode.textContent = `Loaded ${payload.items.length} events`;
      document.getElementById("history-root").innerHTML =
        payload.items.length === 0
          ? '<div class="run-row">No events yet.</div>'
          : payload.items
              .map(
                (item) => `
              <div class="run-row">
                <div>
                  <div>#${item.event_id || "-"} • ${escapeHtml(historyItemDescription(item))}</div>
                  <div class="workflow-footnote">${escapeHtml(formatDateTime(item.created_at))} • reverted: ${item.reverted ? "yes" : "no"}</div>
                </div>
                ${
                  item.reverted
                    ? '<span class="panel-pill">reverted</span>'
                    : `<button class="small-btn history-undo-btn" data-event-id="${item.event_id || 0}">Undo</button>`
                }
              </div>
            `
              )
              .join("");
    }

    return {
      renderSummary,
      canonicalOptionsHtml,
      renderCanonicals,
      queueFilters,
      queueUrl,
      queueSelectedRawNames,
      clearQueueSelection,
      renderQueue,
      renderSuggestions,
      renderMergeCandidates,
      renderQuality,
      historyItemDescription,
      renderHistory,
    };
  }

  global.ManzaraLibraryNormalizationRendering = { create };
})(window);
