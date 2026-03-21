"""Runtime settings for Manzara MVP."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.modules.shayan.config import ShayanSettings, load_shayan_settings


@dataclass(frozen=True)
class Settings:
    """Typed settings with safe defaults for local development."""

    db_path: Path
    shayan: ShayanSettings


def load_settings() -> Settings:
    """Load runtime settings from env with practical local defaults."""
    db_path = Path(os.environ.get("MANZARA_DB_PATH", "data/manzara.db")).expanduser()
    return Settings(db_path=db_path, shayan=load_shayan_settings())
