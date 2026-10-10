"""One complete catalog-native Codex analysis per user-started CLI run."""

from __future__ import annotations

import json
import time
from pathlib import Path


from app.artifacts import workspace_dir
from app.catalog.contracts import CatalogConflict
from app.catalog.publisher_analysis import PUBLISHER_ANALYSIS_CONTRACT
from app.modules.library.publisher_codex import CodexAdapter, CodexSettings, quota_observations
from app.modules.library.publisher_merge_contract import (
    PROMPT_VERSION, build_inventory, build_prompt, inventory_fingerprint, resolve_scope,
    restore_response_ids, validate_clustering_response,
)
from app.task_runtime.contracts import RunContext
from app.task_runtime.logging import redact


def _import(catalog, analysis, separations):
    try:
        if analysis["metadata"].get("prompt_version") != PROMPT_VERSION:
            raise ValueError("Unsupported publisher checkpoint prompt version")
        if inventory_fingerprint(analysis["inventory"]) != analysis["fingerprint"]:
            raise ValueError("Publisher checkpoint inventory fingerprint does not match")
        groups = validate_clustering_response(
            {"clusters": analysis["response"]}, analysis["inventory"], analysis["scope"], separations,
        )
        imported = catalog.import_analysis(analysis["analysis_id"])
    except (ValueError, CatalogConflict) as exc:
        catalog.reject_checkpoint(analysis["analysis_id"], str(exc))
        raise ValueError(
            "Publisher checkpoint conflicts with the current catalog or owner decisions. "
            "Start another analysis after resolving the reported conflict; existing proposals and decisions are preserved. " + str(exc)
        ) from exc
    return groups, imported


def run_analysis(*, db, catalog, settings, workspace, should_stop, log, progress,
                 adapter_factory=CodexAdapter):
    started = time.monotonic()
    state = catalog.state()
    checkpoint = state["checkpoint"]
    if should_stop():
        raise InterruptedError("Publisher analysis cancelled before execution.")
    if checkpoint:
        progress({"phase": "importing publisher checkpoint", "analysis_id": checkpoint["analysis_id"]}, force=True)
        groups, imported = _import(catalog, checkpoint, state["separations"])
        log(f"Publisher checkpoint imported: analysis={checkpoint['analysis_id']}, new_proposals={imported['count']}")
        return summary(groups, checkpoint["scope"], {
            **checkpoint["metadata"], "analysis_id": checkpoint["analysis_id"],
        }, imported, recovered=True)
    progress({"phase": "discovering"}, force=True)
    inventory = build_inventory(db)
    state = catalog.state(inventory)
    scope = resolve_scope(settings.scope, state["successful"])
    log(f"Publisher inventory: entries={len(inventory)}, new_names={sum(row['is_new'] for row in inventory)}, scope={scope}")
    if not inventory or (scope == "new" and not any(entry["is_new"] for entry in inventory)):
        return summary([], scope, {
            "model": settings.model, "reasoning_effort": settings.reasoning_effort,
            "contract_version": PUBLISHER_ANALYSIS_CONTRACT,
            "duration_seconds": time.monotonic() - started,
        }, {"count": 0, "proposal_ids": []}, no_analysis_needed=True)
    if should_stop():
        raise InterruptedError("Publisher analysis cancelled before inference.")
    prompt = build_prompt(inventory, scope, state["separations"])
    progress({"phase": "preparing publisher analysis"}, force=True)
    adapter = adapter_factory(settings, workspace)
    try:
        size = adapter.prepare(prompt)
        if should_stop():
            raise InterruptedError("Publisher analysis cancelled before inference.")
        metadata = {
            "model": settings.model, "reasoning_effort": settings.reasoning_effort,
            "cli_version": adapter.version, "prompt_version": PROMPT_VERSION,
            "contract_version": PUBLISHER_ANALYSIS_CONTRACT, "workspace_path": str(workspace), **size,
        }
        fingerprint = inventory_fingerprint(inventory)
        aid = catalog.create_analysis(inventory, fingerprint, scope, metadata)
        metadata["analysis_id"] = aid
        Path(workspace, "inventory.json").write_text(json.dumps(
            {"fingerprint": fingerprint, "inventory": inventory}, ensure_ascii=False, sort_keys=True,
        ), encoding="utf-8")
        log(f"Publisher analysis started: analysis={aid}, scope={scope}, inventory={len(inventory)}, model={settings.model}")
        progress({"phase": "analyzing publishers", "analysis_id": aid}, force=True)
        before = adapter.telemetry()
        response, usage = adapter.analyze(prompt, should_stop, log=log)
        after = adapter.telemetry()
        if should_stop():
            raise InterruptedError("Publisher analysis cancelled; output was not checkpointed.")
        progress({"phase": "validating publisher response", "analysis_id": aid}, force=True)
        current = catalog.state(inventory)
        groups = validate_clustering_response(
            restore_response_ids(response, inventory), inventory, scope, current["separations"],
        )
        metadata.update(usage)
        metadata.update(duration_seconds=time.monotonic() - started,
                        quota_observations=quota_observations(before, after))
        catalog.checkpoint(aid, groups, metadata)
        log(f"Publisher response checkpointed: analysis={aid}, groups={len(groups)}")
        if should_stop():
            raise InterruptedError("Publisher analysis stopped; the valid response checkpoint will be imported on the next run.")
        progress({"phase": "importing publisher proposals", "analysis_id": aid}, force=True)
        groups, imported = _import(catalog, catalog.get_analysis(aid), current["separations"])
        log(f"Publisher proposals persisted: analysis={aid}, new_proposals={imported['count']}")
        for offset in range(0, len(imported["proposal_ids"]), 100):
            log(f"Publisher proposal IDs: {imported['proposal_ids'][offset:offset + 100]}")
        return summary(groups, scope, metadata, imported)
    finally:
        adapter.close()


def summary(groups, scope, metadata, imported, **flags):
    counts = {}
    for group in groups:
        for key in group["member_ids"]:
            counts[key] = counts.get(key, 0) + 1
    return {
        "kind": "library.publisher_merge_summary", "outcome": "completed",
        "proposal_count": len(groups),
        "cluster_count": sum(group["kind"] == "cluster" for group in groups),
        "singleton_count": sum(group["kind"] == "singleton" for group in groups),
        "unresolved_count": sum(group["kind"] == "unresolved" for group in groups),
        "covered_entries": len(counts),
        "conflicting_groups": sum(any(counts[key] > 1 for key in group["member_ids"]) for group in groups),
        "new_proposals": imported["count"], "proposal_ids": imported["proposal_ids"],
        "publishers_involved": len(counts),
        "uncertain_groups": sum(group["confidence"] == "uncertain" for group in groups),
        "scope": scope, "proposals": groups, **metadata, **flags,
    }


def execute(context: RunContext) -> dict:
    details = {"kind": "library.publisher_merge_summary"}
    try:
        if context.options.limit is not None:
            raise ValueError("Cluster publishers requires the complete inventory; clear the candidate limit.")
        if context.options.per_mime_limit is not None or context.options.only_md5s or context.options.retry_known_failures:
            raise ValueError("Non-PDF cohort/retry options do not apply to Cluster publishers.")
        if context.should_stop():
            raise InterruptedError("Publisher analysis cancelled before execution.")
        catalog = context.db.publisher_catalog()
        catalog.check()
        workspace = workspace_dir("library", "publisher-merges", run_id=context.run_id)
        details["workspace_path"] = str(workspace)
        context.artifact({**details, "outcome": "running"})

        def progress(payload, *, force=False):
            if "analysis_id" in payload:
                details["analysis_id"] = payload["analysis_id"]
            context.progress(payload, force=force)

        with context.db.publisher_analysis_lock():
            result = run_analysis(
                db=context.db, catalog=catalog, settings=CodexSettings.from_config(), workspace=workspace,
                should_stop=context.should_stop, log=context.log, progress=progress,
            )
        # Keep detailed results out of SQLite run summaries and the final terminal line.
        proposals_path = workspace / "proposals.json"
        proposals_path.write_text(json.dumps({
            "analysis_id": result.get("analysis_id"), "scope": result["scope"],
            "proposals": result.pop("proposals"), "new_proposal_ids": result.pop("proposal_ids"),
            "analysis_workspace_path": result.get("workspace_path"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        result["proposals_path"] = str(proposals_path)
        progress({"phase": "publisher proposals ready"}, force=True)
        return {**details, **result}
    except InterruptedError as exc:
        context.log(str(exc))
        return {**details, "outcome": "stopped", "stopped": True, "message": str(exc)}
    except Exception as exc:
        error = redact(exc)
        context.log(f"Publisher clustering failed: {error}", level="ERROR")
        return {**details, "outcome": "failed", "error": error}
