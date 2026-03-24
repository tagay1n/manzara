#!/usr/bin/env python3
"""Run Alembic upgrade head using Python API (stable in this environment)."""

from __future__ import annotations

from alembic import command
from alembic.config import Config


def main() -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    main()

