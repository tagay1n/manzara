"""Shared preview-worker configuration, independent of script entry points."""

import os
from pathlib import Path
from typing import Any, Mapping

from app.artifacts import cache_dir, workspace_dir
from app.document_storage import load_document_storage_settings
from app.modules.library.preview_generation import PreviewGenerationSettings

def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _required(mapping: Mapping[str, Any], key: str, path: str) -> str:
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required config value: {path}.{key}")
    return value


def _run_id() -> int:
    value = str(os.environ.get("MANZARA_TASK_RUN_ID") or "").strip()
    if not value.isdigit() or int(value) <= 0:
        raise RuntimeError("MANZARA_TASK_RUN_ID is required")
    return int(value)


def _resolved_settings(payload: Mapping[str, Any], *, run_id: int) -> tuple[PreviewGenerationSettings, dict[str, str]]:
    documents = _mapping(payload.get("documents"))
    document_storage = load_document_storage_settings(payload)
    settings = PreviewGenerationSettings(
        source_bucket=document_storage.public_bucket,
        target_bucket=(
            document_storage.preview_bucket
            or _required(
                _mapping(_mapping(documents.get("primary_storage")).get("bucket")),
                "book_previews",
                "documents.primary_storage.bucket",
            )
        ),
        cache_dir=Path(
            _required(documents, "cache_path", "documents")
        ).expanduser(),
        workspace=workspace_dir(
            "library", "book-preview-generation", run_id=run_id
        ),
        model_cache_dir=cache_dir("downloaded-models", "huggingface"),
        source_endpoint_url=document_storage.primary.endpoint_url,
        source_region_name=document_storage.primary.region_name,
        encryption_key=_required(payload, "encryption_key", "config"),
        cache_max_bytes=document_storage.cache_max_bytes,
    )
    credentials = {
        "source_access_key_id": document_storage.primary.access_key_id,
        "source_secret_access_key": document_storage.primary.secret_access_key,
        "target_access_key_id": document_storage.primary.access_key_id,
        "target_secret_access_key": document_storage.primary.secret_access_key,
        "target_endpoint_url": document_storage.primary.endpoint_url,
        "target_region_name": document_storage.primary.region_name,
    }
    return settings, credentials
