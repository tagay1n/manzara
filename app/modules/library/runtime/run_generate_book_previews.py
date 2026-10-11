"""Generate previews from SQL-filtered batches through the operations CLI."""

from contextlib import contextmanager

from app.task_runtime.contracts import RunContext


@contextmanager
def _resources(context):
    """Initialize expensive resources only for the first nonempty batch."""
    from app.document_storage import load_document_storage_settings, prune_document_cache
    from app.modules.library.preview_detection import DocLayNetPageDetector
    from app.modules.library.preview_runtime import resolved_settings
    from app.operational_state import OperationalStateStore
    from app.runtime_config import load_runtime_config
    from app.s3_transfer import create_s3_client

    storage = load_document_storage_settings(load_runtime_config())
    settings = resolved_settings(storage, run_id=context.run_id)
    prune_document_cache(storage.cache_path, max_bytes=storage.cache_max_bytes)
    s3 = create_s3_client(storage.primary, profile="previews")
    try:
        context.log("library previews: checking public preview storage")
        s3.head_bucket(Bucket=storage.preview_bucket)
        if context.should_stop():
            yield None
            return
        context.log("library previews: loading pinned CPU page detector")
        detector = DocLayNetPageDetector.from_huggingface(cache_dir=settings.model_cache_dir)
        yield dict(runtime=OperationalStateStore(context.db.local_state_path), storage=storage,
                   settings=settings, s3=s3, detector=detector)
    finally:
        s3.close()


def execute(context: RunContext) -> dict:
    if context.options.per_mime_limit is not None:
        raise ValueError("Book previews do not support per-MIME limits")
    if context.should_stop():
        return {"kind": "library.catalog_preview_summary", "outcome": "stopped"}
    from app.catalog.book_previews import BookPreviewCatalogStore
    from app.modules.library.catalog_preview_worker import run_previews
    from app.modules.library.previews import PREVIEW_RECIPE_VERSION
    from app.postgres_engine import acquire_postgres_engine, release_postgres_engine

    engine = acquire_postgres_engine(context.db.database_url, schema=context.db.schema)
    try:
        store = BookPreviewCatalogStore(engine, schema=context.db.schema)
        store.preflight(recipe=PREVIEW_RECIPE_VERSION)
        return run_previews(store, context=context, load_resources=lambda: _resources(context))
    finally:
        release_postgres_engine(engine)
