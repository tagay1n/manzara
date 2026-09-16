"""Event-driven coordinator for cached task-attention signals."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable, Iterable


SignalLoader = Callable[[int], list[dict[str, Any]]]


@dataclass(frozen=True)
class AttentionProvider:
    provider_id: str
    load: SignalLoader
    invalidating_events: frozenset[str]
    invalidating_tasks: frozenset[str] = frozenset()


_RERUN_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "maintenance.monocorpus_sync": (
        "library.prepare_document_cleanup",
        "library.metadata_extract",
        "library.metadata_validate",
        "library.collection_detect",
        "library.site_export",
    ),
    "maintenance.sync_documents_s3": (
        "library.metadata_extract",
        "library.extract_non_pdf",
        "library.generate_book_previews",
    ),
    "library.metadata_extract": (
        "maintenance.monocorpus_meta_evaluate",
        "library.metadata_validate",
    ),
    "maintenance.monocorpus_meta_evaluate": (
        "library.collection_detect",
        "library.generate_book_previews",
        "library.personality_suggestions_refresh",
        "library.publisher_suggestions_refresh",
        "library.site_export",
    ),
    "library.collection_apply": ("library.site_export",),
    "library.generate_book_previews": ("library.site_export",),
    "library.extract_non_pdf": ("library.site_export",),
}

_TASK_LABELS = {
    "library.prepare_document_cleanup": "Document cleanup scan recommended",
    "library.metadata_validate": "Metadata validation recommended",
    "library.collection_detect": "Collection discovery recommended",
    "library.site_export": "Static Library export recommended",
    "library.metadata_extract": "Metadata extraction available",
    "library.extract_non_pdf": "Non-PDF extraction available",
    "library.generate_book_previews": "Book preview generation available",
    "maintenance.monocorpus_meta_evaluate": "Metadata evaluation available",
    "library.personality_suggestions_refresh": "Personality suggestions may need refresh",
    "library.publisher_suggestions_refresh": "Publisher suggestions may need refresh",
    "library.collection_apply": "Collection overrides ready to apply",
}


class AttentionService:
    """Tail local events and refresh durable-source counts off the request path."""

    def __init__(
        self,
        db: Any,
        providers: Iterable[AttentionProvider],
        *,
        initial_delay_seconds: float = 5.0,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        self.db = db
        self.providers = {item.provider_id: item for item in providers}
        self.initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self.poll_interval_seconds = max(0.01, float(poll_interval_seconds))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._pending = set(self.providers)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        source_event_id = self.db.get_latest_event_id()
        for provider_id in self.providers:
            self.db.mark_attention_provider_stale(
                provider_id, source_event_id=source_event_id
            )
        self._thread = threading.Thread(
            target=self._run, name="attention-coordinator", daemon=True
        )
        self._thread.start()

    def shutdown(self, timeout_seconds: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout_seconds)))
        self._thread = None

    def snapshot(self) -> dict[str, Any]:
        return self.db.get_attention_snapshot()

    def process_events_once(self) -> None:
        with self._lock:
            cursor = self.db.get_attention_event_cursor()
            while True:
                events = self.db.get_events_after(cursor, limit=200)
                if not events:
                    return
                for event in events:
                    event_id = int(event.get("event_id") or 0)
                    self._apply_event(event)
                    cursor = max(cursor, event_id)
                self.db.set_attention_event_cursor(cursor)
                if len(events) < 200:
                    return

    def _apply_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        task_id = str(event.get("task_id") or "")
        event_id = int(event.get("event_id") or 0)
        panel_id = str(event.get("panel_id") or "")
        changed = False

        for provider in self.providers.values():
            if event_type not in provider.invalidating_events:
                continue
            if provider.invalidating_tasks and task_id not in provider.invalidating_tasks:
                continue
            self._pending.add(provider.provider_id)
            self.db.mark_attention_provider_stale(
                provider.provider_id, source_event_id=event_id
            )

        if event_type == "task.failed" and task_id:
            changed = self.db.upsert_attention_dot(
                f"runtime.failed.{task_id}",
                task_id=task_id,
                panel_id=panel_id or task_id.split(".", 1)[0],
                label="Latest run failed",
                href=f"/tasks/{task_id}",
                source_event_id=event_id,
            )
        elif event_type == "task.completed" and task_id:
            changed = self.db.delete_attention_signal(
                f"runtime.failed.{task_id}"
            ) or changed
            changed = self.db.delete_attention_signal(
                f"runtime.rerun.{task_id}"
            ) or changed
            for dependent in _RERUN_DEPENDENCIES.get(task_id, ()):
                changed = self.db.upsert_attention_dot(
                    f"runtime.rerun.{dependent}",
                    task_id=dependent,
                    panel_id=self._panel_for_task(dependent),
                    label=_TASK_LABELS.get(dependent, "Rerun recommended"),
                    href=f"/tasks/{dependent}",
                    source_event_id=event_id,
                ) or changed
        elif event_type in {
            "library.collections.updated",
            "library.document_cleanup_changed",
        }:
            self._pending.update(self.providers)
            for provider_id in self.providers:
                self.db.mark_attention_provider_stale(
                    provider_id, source_event_id=event_id
                )
            dependents = (
                ("library.collection_apply", "library.site_export")
                if event_type == "library.collections.updated"
                else ("library.site_export",)
            )
            for dependent in dependents:
                changed = self.db.upsert_attention_dot(
                    f"runtime.rerun.{dependent}",
                    task_id=dependent,
                    panel_id=self._panel_for_task(dependent),
                    label=_TASK_LABELS.get(dependent, "Rerun recommended"),
                    href=f"/tasks/{dependent}",
                    source_event_id=event_id,
                ) or changed
        if changed:
            self.db.insert_event(
                "attention.updated",
                task_id=None,
                run_id=None,
                panel_id=None,
                payload={"provider_id": "runtime"},
            )

    @staticmethod
    def _panel_for_task(task_id: str) -> str:
        if task_id in {"library.metadata_validate", "maintenance.monocorpus_meta_evaluate"}:
            return "metadata"
        if task_id == "library.prepare_document_cleanup":
            return "maintenance"
        if task_id.startswith("library.collection_"):
            return "collections"
        return "library" if task_id.startswith("library.") else "maintenance"

    def _refresh_one(self) -> bool:
        with self._lock:
            if not self._pending:
                return False
            provider_id = sorted(self._pending)[0]
            self._pending.remove(provider_id)
        provider = self.providers[provider_id]
        source_event_id = self.db.get_latest_event_id()
        try:
            signals = provider.load(source_event_id)
            changed = self.db.replace_attention_provider_signals(
                provider_id, signals, source_event_id=source_event_id
            )
        except Exception as exc:  # noqa: BLE001 - provider boundary
            self.db.set_attention_provider_error(
                provider_id, f"{type(exc).__name__}: {exc}",
                source_event_id=source_event_id,
            )
            return True
        if changed:
            self.db.insert_event(
                "attention.updated",
                task_id=None,
                run_id=None,
                panel_id=None,
                payload={"provider_id": provider_id},
            )
        return True

    def _run(self) -> None:
        if self._stop.wait(self.initial_delay_seconds):
            return
        while not self._stop.is_set():
            try:
                self.process_events_once()
                refreshed = self._refresh_one()
            except Exception:
                refreshed = False
            if not refreshed:
                self._wake.wait(self.poll_interval_seconds)
                self._wake.clear()


__all__ = ["AttentionProvider", "AttentionService"]
