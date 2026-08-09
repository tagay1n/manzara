"""Discover path-independent Library collection proposals."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.modules.library.collection_constants import COLLECTIONS_PANEL_ID  # noqa: E402
from app.modules.library.collection_detection import discover_collections  # noqa: E402
from app.run_artifact_channel import emit_run_artifact  # noqa: E402
from app.settings import load_settings  # noqa: E402


def main() -> None:
    argparse.ArgumentParser(
        description="Discover Library collection proposals"
    ).parse_args()
    stop = {"requested": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("requested", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("requested", True))
    run_id = int(os.environ.get("MANZARA_TASK_RUN_ID") or 0)
    settings = load_settings()
    db = Database(settings.database_url, schema=settings.database_schema)

    def publish(progress: dict) -> None:
        if not run_id:
            return
        snapshot = {"status": "running", **progress}
        db.update_run_progress(run_id, snapshot)
        db.insert_event(
            "task.progress",
            task_id="library.collection_detect",
            run_id=run_id,
            panel_id=COLLECTIONS_PANEL_ID,
            payload={"status": "running", "progress": snapshot},
        )

    print(
        "library collection discovery: start metadata_scope=all path_evidence=false",
        flush=True,
    )
    payload = discover_collections(
        should_stop=lambda: bool(stop["requested"]),
        on_progress=publish,
    )
    payload["kind"] = "library.collection_discovery_summary"
    payload["stopped"] = bool(stop["requested"])
    if run_id:
        terminal_status = (
            "failed"
            if not payload.get("available")
            else "stopped"
            if payload["stopped"]
            else "completed"
        )
        db.update_run_progress(
            run_id,
            {
                "status": terminal_status,
                **payload,
            },
        )
        db.insert_event(
            "task.progress",
            task_id="library.collection_detect",
            run_id=run_id,
            panel_id=COLLECTIONS_PANEL_ID,
            payload={
                "status": terminal_status,
                "progress": payload,
            },
        )
    emit_run_artifact(payload)
    print(
        f"library collection discovery: final {json.dumps(payload, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )
    if not payload.get("available"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
