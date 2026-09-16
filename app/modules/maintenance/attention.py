"""Maintenance-owned exact attention counts."""

from __future__ import annotations

from typing import Any

from app.document_storage import load_document_storage_settings
from app.modules.maintenance.content_storage_migration_repository import (
    ContentStorageMigrationRepository,
)
from app.modules.maintenance.document_sync_repository import (
    PostgresDocumentSyncRepository,
)
from app.runtime_config import load_runtime_config


def load_maintenance_signals(settings: Any, _source_event_id: int) -> list[dict[str, Any]]:
    storage = load_document_storage_settings(load_runtime_config())
    uploads = PostgresDocumentSyncRepository(
        settings.database_url, schema=settings.database_schema
    )
    migration = ContentStorageMigrationRepository(
        settings.database_url,
        schema=settings.database_schema,
        legacy_endpoint=storage.legacy.endpoint_url,
        legacy_bucket=storage.legacy_content_bucket,
    )
    try:
        upload_count = uploads.count_pending_documents()
        migration_count = migration.count_pending()
    finally:
        uploads.dispose()
        migration.dispose()
    return [
        {
            "signal_id": "maintenance.documents.upload",
            "task_id": "maintenance.sync_documents_s3",
            "panel_id": "maintenance",
            "section_id": None,
            "kind": "count",
            "count": upload_count,
            "label": "Documents awaiting upload",
            "href": "/tasks/maintenance.sync_documents_s3",
        },
        {
            "signal_id": "maintenance.pdf_content.migrate",
            "task_id": "maintenance.migrate_pdf_content",
            "panel_id": "library",
            "section_id": "overview",
            "kind": "count",
            "count": migration_count,
            "label": "PDF content awaiting migration",
            "href": "/tasks/maintenance.migrate_pdf_content",
        },
    ]


__all__ = ["load_maintenance_signals"]
