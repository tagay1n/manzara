"""Entry point for embedded monocorpus sync runtime."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    runtime_root = Path(__file__).resolve().parent
    sys.path.insert(0, str(runtime_root))
    from sync.service import sync

    sync()


if __name__ == "__main__":
    main()
