"""Cleanup-plan runtime behavior at the PostgreSQL progress boundary."""

from __future__ import annotations

import psycopg2
import pytest

from app.modules.library.runtime import run_prepare_document_cleanup as runtime


class _Database:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def publish_run_progress(self, **_kwargs) -> None:
        raise self.error


def test_progress_slot_exhaustion_does_not_abort_cleanup_plan() -> None:
    runtime._publish_progress(
        _Database(psycopg2.OperationalError("remaining connection slots")),
        run_id=42,
        current=1000,
        total=70_000,
        counters={"plans_created": 1},
    )


def test_non_database_progress_failure_remains_actionable() -> None:
    with pytest.raises(ValueError, match="bad progress"):
        runtime._publish_progress(
            _Database(ValueError("bad progress")),
            run_id=42,
            current=1000,
            total=70_000,
            counters={},
        )
