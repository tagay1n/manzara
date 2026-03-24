"""Maintenance module configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MaintenanceSettings:
    """Paths required by the maintenance panel integration."""

    monocorpus_repo_path: Path
    pgbackrest_stanza: str


def load_maintenance_settings() -> MaintenanceSettings:
    """Load maintenance settings from environment with local defaults."""
    repo_default = Path("/home/tans1q/projects/monocorpus")
    repo_path = Path(
        os.environ.get("MONOCORPUS_REPO_PATH", str(repo_default))
    ).expanduser()
    pgbackrest_stanza = str(os.environ.get("PG_BACKREST_STANZA", "monocorpus")).strip() or "monocorpus"

    return MaintenanceSettings(
        monocorpus_repo_path=repo_path,
        pgbackrest_stanza=pgbackrest_stanza,
    )
