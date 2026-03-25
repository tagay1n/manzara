"""Oscar module configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.artifacts import flow_artifacts_dir


@dataclass(frozen=True)
class OscarSettings:
    """Paths and defaults required by Oscar flow tasks."""

    repo_path: Path
    artifacts_dir: Path
    parquet_part_size_mb: int


def load_oscar_settings() -> OscarSettings:
    """Load Oscar settings from environment with local defaults."""
    repo_default = Path("/home/tans1q/projects/oscar-corpus-extractor")
    repo_path = Path(os.environ.get("OSCAR_REPO_PATH", str(repo_default))).expanduser()
    artifacts_dir = Path(
        os.environ.get("OSCAR_ARTIFACTS_DIR", str(flow_artifacts_dir("oscar")))
    ).expanduser()
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    raw_part_size = str(os.environ.get("OSCAR_PARQUET_PART_SIZE_MB", "1024")).strip() or "1024"
    try:
        parquet_part_size_mb = max(1, int(raw_part_size))
    except ValueError:
        parquet_part_size_mb = 1024

    return OscarSettings(
        repo_path=repo_path,
        artifacts_dir=artifacts_dir,
        parquet_part_size_mb=parquet_part_size_mb,
    )

