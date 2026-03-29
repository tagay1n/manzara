"""Helpers for first-class structured run artifact exchange."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict


RUN_ARTIFACT_PATH_ENV = "MANZARA_RUN_ARTIFACT_PATH"


def emit_run_artifact(payload: Dict[str, Any]) -> bool:
    """Persist one structured artifact payload to runtime-provided path."""
    if not isinstance(payload, dict) or not payload:
        return False
    raw_path = str(os.environ.get(RUN_ARTIFACT_PATH_ENV) or "").strip()
    if not raw_path:
        return False
    target = Path(raw_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp_path.replace(target)
    return True


def read_run_artifact(path: Path) -> Dict[str, Any]:
    """Read one artifact JSON payload from file path."""
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}
