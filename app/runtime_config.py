"""Shared loader for local runtime YAML configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml


def _contains_redacted(node: Any) -> bool:
    if isinstance(node, str):
        return "<REDACTED>" in node
    if isinstance(node, dict):
        return any(_contains_redacted(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_redacted(value) for value in node)
    return False


def load_runtime_config() -> Dict[str, Any]:
    """Load the first usable local config without ever using the masked example."""
    override = str(os.environ.get("MANZARA_CONFIG_PATH") or "").strip()
    repo_root = Path(__file__).resolve().parent.parent
    candidates = (
        [Path(override).expanduser()]
        if override
        else [repo_root / "config.local.yaml", repo_root / "config.yaml"]
    )
    for path in candidates:
        if not path.exists():
            continue
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(payload, dict) and not _contains_redacted(payload):
            return payload
    return {}
