"""Thread-safe metadata evaluation progress publication."""

from __future__ import annotations

import threading
from collections import Counter

from app.db import Database

TASK_ID = "maintenance.monocorpus_meta_evaluate"


PANEL_ID = "metadata"


class _EvaluationProgress:
    """Publish evaluation progress using the shared task progress contract."""

    def __init__(self, db: Database, *, run_id: int | None, total: int) -> None:
        self.db = db
        self.run_id = run_id
        self.total = max(0, int(total))
        self.current = 0
        self.counters: Counter[str] = Counter(
            succeeded=0,
            rules_skipped=0,
            terminal=0,
            quota_deferred=0,
            service_deferred=0,
        )
        self.model_attempts: Counter[str] = Counter()
        self.model_successes: Counter[str] = Counter()
        self.lock = threading.Lock()

    def publish(self) -> None:
        with self.lock:
            self._publish_locked()

    def record_model_attempt(self, model_name: str) -> None:
        with self.lock:
            self.model_attempts[str(model_name)] += 1
            self._publish_locked()

    def record_completed(
        self,
        outcome: str,
        *,
        model_name: str | None = None,
        count: int = 1,
    ) -> None:
        amount = max(0, int(count))
        if amount == 0:
            return
        with self.lock:
            self.current += amount
            self.counters[str(outcome)] += amount
            if model_name:
                self.model_successes[str(model_name)] += amount
            self._publish_locked()

    def _publish_locked(self) -> None:
        if self.run_id is None:
            return
        resolved = sum(
            int(self.counters.get(key, 0))
            for key in ("succeeded", "rules_skipped", "terminal")
        )
        payload = {
            "current": self.current,
            "total": self.total,
            "percent": (
                100.0
                if self.total == 0
                else round((self.current / self.total) * 100, 2)
            ),
            "remaining": max(0, self.total - resolved),
            **dict(self.counters),
            "model_attempts": dict(self.model_attempts),
            "model_successes": dict(self.model_successes),
        }
        self.db.publish_run_progress(
            task_id=TASK_ID,
            run_id=self.run_id,
            panel_id=PANEL_ID,
            progress=payload,
        )
