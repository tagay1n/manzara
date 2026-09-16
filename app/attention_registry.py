"""Application assembly for flow-owned attention providers."""

from __future__ import annotations

from typing import Any

from app.attention import AttentionProvider
from app.modules.library.attention import (
    load_library_queue_signals,
    load_preview_signals,
)
from app.modules.maintenance.attention import load_maintenance_signals


TASK_ATTENTION_POLICIES = {
    "maintenance.migrate_pdf_content": "count",
    "maintenance.monocorpus_sync": "external",
    "maintenance.sync_documents_s3": "count",
    "maintenance.monocorpus_meta_evaluate": "dot",
    "library.personality_suggestions_refresh": "dot",
    "library.publisher_suggestions_refresh": "dot",
    "library.site_export": "dot",
    "library.metadata_validate": "dot",
    "library.extract_non_pdf": "dot",
    "library.metadata_extract": "dot",
    "library.prepare_document_cleanup": "count_and_dot",
    "library.generate_book_previews": "count",
    "library.collection_detect": "dot",
    "library.collection_validate": "count",
    "library.collection_apply": "dot",
}


def build_attention_providers(settings: Any) -> list[AttentionProvider]:
    terminal = frozenset({"task.completed", "task.failed", "task.stopped"})
    return [
        AttentionProvider(
            "library.queues",
            lambda cursor: load_library_queue_signals(settings, cursor),
            terminal
            | frozenset(
                {
                    "library.collections.updated",
                    "library.document_cleanup_changed",
                }
            ),
            frozenset(
                {
                    "library.prepare_document_cleanup",
                    "library.collection_detect",
                    "library.collection_validate",
                    "library.collection_apply",
                }
            ),
        ),
        AttentionProvider(
            "library.previews",
            lambda cursor: load_preview_signals(settings, cursor),
            terminal,
            frozenset(
                {
                    "library.generate_book_previews",
                    "maintenance.monocorpus_meta_evaluate",
                    "maintenance.sync_documents_s3",
                }
            ),
        ),
        AttentionProvider(
            "maintenance.documents",
            lambda cursor: load_maintenance_signals(settings, cursor),
            terminal,
            frozenset(
                {
                    "maintenance.monocorpus_sync",
                    "maintenance.sync_documents_s3",
                    "maintenance.migrate_pdf_content",
                }
            ),
        ),
    ]


__all__ = ["TASK_ATTENTION_POLICIES", "build_attention_providers"]
