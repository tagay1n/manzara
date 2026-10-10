"""Preview settings resolved from the shared storage contract and explicit run ID."""

from app.artifacts import cache_dir, workspace_dir
from app.document_storage import DocumentStorageSettings
from app.modules.library.preview_generation import PreviewGenerationSettings


def resolved_settings(storage: DocumentStorageSettings, *, run_id: int) -> PreviewGenerationSettings:
    if not storage.preview_bucket:
        raise RuntimeError("documents.primary_storage.bucket.book_previews is required")
    if storage.preview_bucket in {storage.public_bucket, storage.private_bucket}:
        raise RuntimeError("book_previews must be a dedicated public preview bucket")
    return PreviewGenerationSettings(
        target_bucket=storage.preview_bucket,
        cache_dir=storage.cache_path,
        workspace=workspace_dir("library", "book-preview-generation", run_id=run_id),
        model_cache_dir=cache_dir("downloaded-models", "huggingface"),
        cache_max_bytes=storage.cache_max_bytes,
    )
