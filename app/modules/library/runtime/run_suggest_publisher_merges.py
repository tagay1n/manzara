"""One complete Codex publisher identity analysis per user-started run."""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from app.artifacts import workspace_dir
from app.db import Database
from app.modules.library.publisher_codex import (
    CodexAdapter,
    CodexSettings,
    quota_observations,
)
from app.modules.library.publisher_merge_contract import (
    PROMPT_VERSION,
    build_inventory,
    build_prompt,
    inventory_fingerprint,
    resolve_scope,
    restore_response_ids,
    validate_clustering_response,
)
from app.run_artifact_channel import emit_run_artifact
from app.settings import load_settings

TASK_ID = "library.suggest_publisher_merges"


def run_analysis(*, db, settings, workspace, should_stop, adapter_factory=CodexAdapter):
    started = time.monotonic()
    state = db.publisher_analysis_state()
    checkpoint = state["checkpoint"]
    if checkpoint:
        try:
            groups = validate_clustering_response(
                {"clusters": checkpoint["response"]},
                checkpoint["inventory"],
                checkpoint["scope"],
                state["separations"],
            )
            count = db.import_publisher_analysis(checkpoint["analysis_id"])
        except ValueError as exc:
            db.reject_publisher_checkpoint(checkpoint["analysis_id"], str(exc))
            raise ValueError(
                "Completed publisher checkpoint conflicts with current owner decisions. Start another analysis; prior review decisions are preserved."
            ) from exc
        return summary(
            groups, checkpoint["scope"], checkpoint["metadata"], count, recovered=True
        )
    inventory = build_inventory(db)
    scope = resolve_scope(settings.scope, state["successful"])
    if scope == "new" and not any(entry["is_new"] for entry in inventory):
        return summary(
            [],
            scope,
            {
                "model": settings.model,
                "reasoning_effort": settings.reasoning_effort,
                "duration_seconds": time.monotonic() - started,
            },
            0,
            no_analysis_needed=True,
        )
    if should_stop():
        raise InterruptedError("Publisher analysis cancelled before inference.")
    prompt = build_prompt(inventory, scope, state["separations"])
    adapter = adapter_factory(settings, workspace)
    try:
        size = adapter.prepare(prompt)
        metadata = {
            "model": settings.model,
            "reasoning_effort": settings.reasoning_effort,
            "cli_version": adapter.version,
            "prompt_version": PROMPT_VERSION,
            **size,
        }
        fingerprint = inventory_fingerprint(inventory)
        aid = db.create_publisher_analysis(inventory, fingerprint, scope, metadata)
        Path(workspace, "inventory.json").write_text(
            json.dumps(
                {"fingerprint": fingerprint, "inventory": inventory},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        print(
            f"Publisher analysis started: analysis={aid}, scope={scope}, inventory={len(inventory)}, model={settings.model}",
            flush=True,
        )
        before = adapter.telemetry()
        response, usage = adapter.analyze(prompt, should_stop)
        after = adapter.telemetry()
        if should_stop():
            raise InterruptedError(
                "Publisher analysis cancelled; output was not published."
            )
        current = db.publisher_analysis_state()
        groups = validate_clustering_response(
            restore_response_ids(response, inventory),
            inventory,
            scope,
            current["separations"],
        )
        metadata.update(usage)
        metadata.update(
            duration_seconds=time.monotonic() - started,
            quota_observations=quota_observations(before, after),
        )
        db.checkpoint_publisher_analysis(aid, groups, metadata)
        print(
            f"Publisher response checkpointed: analysis={aid}, groups={len(groups)}",
            flush=True,
        )
        count = db.import_publisher_analysis(aid)
        return summary(groups, scope, metadata, count)
    finally:
        adapter.close()


def recover_completed_response(*, db, analysis_id, workspace):
    """Import a verified completed CLI artifact, without constructing an adapter."""
    if type(analysis_id) is not int or analysis_id <= 0:
        raise ValueError("analysis_id must be a positive integer")
    analysis = db.get_publisher_analysis(analysis_id)
    state = db.publisher_analysis_state()
    metadata = dict(analysis["metadata"])
    if analysis["state"] in {"checkpointed", "imported"}:
        groups = validate_clustering_response(
            {"clusters": analysis["response"]},
            analysis["inventory"],
            analysis["scope"],
            state["separations"],
        )
    elif analysis["state"] == "generating":
        workspace = Path(workspace)
        snapshot = json.loads((workspace / "inventory.json").read_text())
        if (
            snapshot.get("inventory") != analysis["inventory"]
            or snapshot.get("fingerprint") != analysis["fingerprint"]
            or inventory_fingerprint(snapshot["inventory"]) != analysis["fingerprint"]
        ):
            raise ValueError("Saved publisher inventory does not match the analysis")
        if metadata.get("prompt_version") != PROMPT_VERSION:
            raise ValueError("Unsupported saved publisher prompt version")
        events = []
        for line in (workspace / "lifecycle.jsonl").read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") in {"turn.completed", "turn.failed"}:
                events.append(event)
        if not events or events[-1]["type"] != "turn.completed":
            raise ValueError("Saved Codex analysis did not complete")
        payload = json.loads((workspace / "response.json").read_text())
        groups = validate_clustering_response(
            restore_response_ids(payload, analysis["inventory"]),
            analysis["inventory"],
            analysis["scope"],
            state["separations"],
        )
        usage = events[-1].get("usage")
        metadata.update(
            reported_token_usage=usage if isinstance(usage, dict) else None,
            recovered_from_workspace=str(workspace),
        )
        db.checkpoint_publisher_analysis(analysis_id, groups, metadata)
    else:
        raise ValueError("Publisher analysis is not recoverable")
    count = db.import_publisher_analysis(analysis_id)
    return summary(groups, analysis["scope"], metadata, count, recovered=True)


def summary(groups, scope, metadata, count, **flags):
    counts = {}
    for group in groups:
        for key in group["member_ids"]:
            counts[key] = counts.get(key, 0) + 1
    conflicting_groups = sum(
        any(counts[key] > 1 for key in group["member_ids"]) for group in groups
    )
    return {
        "kind": "library.publisher_merge_summary",
        "proposal_count": len(groups),
        "cluster_count": sum(group["kind"] == "cluster" for group in groups),
        "singleton_count": sum(group["kind"] == "singleton" for group in groups),
        "unresolved_count": sum(group["kind"] == "unresolved" for group in groups),
        "covered_entries": len(counts),
        "conflicting_groups": conflicting_groups,
        "new_proposals": count,
        "publishers_involved": len(
            {member for group in groups for member in group["member_ids"]}
        ),
        "uncertain_groups": sum(group["confidence"] == "uncertain" for group in groups),
        "scope": scope,
        "review_link": "/library/publishers",
        **metadata,
        **flags,
    }


def main():
    argparse.ArgumentParser(
        description="Suggest publisher merges using subscription Codex; never apply changes."
    ).parse_args()
    settings = load_settings()
    db = Database(
        settings.database_url,
        schema=settings.database_schema,
        # The session advisory lock occupies one connection for the whole analysis.
        # TaskRunner normally limits task pools to one; catalog queries need a second.
        pool_size=max(2, settings.database_pool_size),
        local_state_path=settings.local_state_path,
    )
    stopped = {"value": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.__setitem__("value", True))
    run_id = (
        int(os.environ["MANZARA_TASK_RUN_ID"])
        if os.environ.get("MANZARA_TASK_RUN_ID")
        else None
    )
    workspace = workspace_dir("library", "publisher-merges", run_id=run_id)
    try:
        with db.publisher_analysis_lock():
            result = run_analysis(
                db=db,
                settings=CodexSettings.from_config(),
                workspace=workspace,
                should_stop=lambda: stopped["value"],
            )
        emit_run_artifact(result)
        print(
            "Publisher analysis completed: " + json.dumps(result, ensure_ascii=False),
            flush=True,
        )
    except InterruptedError as exc:
        emit_run_artifact(
            {
                "kind": "library.publisher_merge_summary",
                "stopped": True,
                "message": str(exc),
                "review_link": "/library/publishers",
            }
        )
        print(str(exc), flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
