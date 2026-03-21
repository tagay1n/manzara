"""Runtime settings for Manzara MVP."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.modules.maintenance.config import (
    MaintenanceSettings,
    load_maintenance_settings,
)
from app.modules.shayan.config import ShayanSettings, load_shayan_settings


@dataclass(frozen=True)
class Settings:
    """Typed settings with safe defaults for local development."""

    db_path: Path
    shayan: ShayanSettings
    maintenance: MaintenanceSettings
    scheduler_enabled: bool


def load_settings() -> Settings:
    """Load runtime settings from env with practical local defaults."""
    db_path = Path(os.environ.get("MANZARA_DB_PATH", "data/manzara.db")).expanduser()
    scheduler_enabled = os.environ.get("MANZARA_ENABLE_SCHEDULER", "1").strip() not in {
        "0",
        "false",
        "False",
    }
    return Settings(
        db_path=db_path,
        shayan=load_shayan_settings(),
        maintenance=load_maintenance_settings(),
        scheduler_enabled=scheduler_enabled,
    )
