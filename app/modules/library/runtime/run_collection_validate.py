"""Validate collection proposals through the shared Gemini runtime."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.modules.library.collection_validation import validate_collection_proposals  # noqa: E402
from app.run_artifact_channel import emit_run_artifact  # noqa: E402
from app.settings import load_settings  # noqa: E402


def _excerpt(md5: str) -> str | None:
    archive = Path("~/.monocorpus/1_result").expanduser() / f"{md5}.zip"
    if not archive.exists():
        return None
    try:
        with zipfile.ZipFile(archive) as source:
            names = [name for name in source.namelist() if name.lower().endswith(".md")]
            if not names:
                return None
            value = source.read(names[0]).decode("utf-8", errors="replace")
        normalized = "\n".join(
            line.strip() for line in value.splitlines() if line.strip()
        )
        return normalized[:2000] or None
    except (OSError, zipfile.BadZipFile):
        return None


def main() -> None:
    argparse.ArgumentParser(
        description="Validate Library collection proposals with Gemini"
    ).parse_args()
    stop = {"requested": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("requested", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("requested", True))
    run_id = int(os.environ.get("MANZARA_TASK_RUN_ID") or 0)
    if run_id <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    settings = load_settings()
    db = Database(settings.database_url, schema=settings.database_schema)
    print("library collection validation: start max_batch=20 adaptive=true", flush=True)
    summary = validate_collection_proposals(
        db,
        run_id=run_id,
        should_stop=lambda: bool(stop["requested"]),
        excerpt_loader=_excerpt,
    )
    emit_run_artifact(summary)
    print(
        f"library collection validation: final {json.dumps(summary, ensure_ascii=False, sort_keys=True)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
