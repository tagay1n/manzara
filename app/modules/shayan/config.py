"""Shayan module configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ShayanSettings:
    """Paths required by the Shayan integration."""

    repo_path: Path
    output_path: Path
    status_file: Path
    summary_file: Path
    latest_snapshot_file: Path


def load_shayan_settings() -> ShayanSettings:
    """Load Shayan settings from environment with local defaults."""
    repo_default = Path("/home/tans1q/projects/shayan-video-downloader")
    output_default = Path("/home/tans1q/video-archive")

    repo_path = Path(os.environ.get("SHAYAN_REPO_PATH", str(repo_default))).expanduser()
    output_path = Path(os.environ.get("SHAYAN_OUTPUT_PATH", str(output_default))).expanduser()

    return ShayanSettings(
        repo_path=repo_path,
        output_path=output_path,
        status_file=repo_path / "_artifacts" / "status.json",
        summary_file=repo_path / "_artifacts" / "last-main-run-summary.json",
        latest_snapshot_file=repo_path / "_artifacts" / "snapshots" / "latest.json",
    )
