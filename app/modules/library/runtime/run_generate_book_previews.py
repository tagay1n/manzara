"""Generate missing book previews through the interactive operations CLI."""

from app.s3_transfer import s3_client_config
from app.task_runtime.contracts import RunContext


def execute(context: RunContext) -> dict:
    if context.options.per_mime_limit is not None:
        raise ValueError("Book previews do not support per-MIME limits")
    if context.should_stop():
        return {"kind": "library.catalog_preview_summary", "outcome": "stopped"}
    from app.catalog.book_previews import BookPreviewCatalogStore
    from app.postgres_engine import acquire_postgres_engine, release_postgres_engine

    engine = acquire_postgres_engine(context.db.database_url, schema=context.db.schema)
    try:
        store = BookPreviewCatalogStore(engine, schema=context.db.schema)
        from app.modules.library.previews import PREVIEW_RECIPE_VERSION

        store.preflight(recipe=PREVIEW_RECIPE_VERSION)
        candidates = store.list_candidates(
            recipe=PREVIEW_RECIPE_VERSION, limit=context.options.limit,
            only_md5s=context.options.only_md5s,
            retry_known_failures=context.options.retry_known_failures,
        )
        context.log(f"library previews: discovered candidates={len(candidates)} recipe={PREVIEW_RECIPE_VERSION}")
        context.progress({"phase": "discovering", "current": 0, "total": len(candidates)}, force=True)
        if not candidates or context.should_stop():
            return {"kind": "library.catalog_preview_summary", "total": len(candidates), "processed": 0,
                    "ready": 0, "failed": 0, "stopped": context.should_stop(),
                    "outcome": "stopped" if context.should_stop() else "completed"}
        from boto3 import Session

        from app.document_storage import (
            load_document_storage_settings,
            prune_document_cache,
        )
        from app.modules.library.catalog_preview_worker import run_previews
        from app.modules.library.preview_detection import DocLayNetPageDetector
        from app.modules.library.preview_runtime import resolved_settings
        from app.operational_state import OperationalStateStore
        from app.runtime_config import load_runtime_config

        if context.should_stop():
            return {"kind": "library.catalog_preview_summary", "outcome": "stopped"}
        storage = load_document_storage_settings(load_runtime_config())
        settings = resolved_settings(storage, run_id=context.run_id)
        prune_document_cache(storage.cache_path, max_bytes=storage.cache_max_bytes)
        s3 = Session().client(
            "s3", endpoint_url=storage.primary.endpoint_url, region_name=storage.primary.region_name,
            aws_access_key_id=storage.primary.access_key_id,
            aws_secret_access_key=storage.primary.secret_access_key,
            config=s3_client_config("previews"),
        )
        try:
            context.log("library previews: checking public preview storage")
            s3.head_bucket(Bucket=storage.preview_bucket)
            if context.should_stop():
                return {"kind": "library.catalog_preview_summary", "outcome": "stopped"}
            context.log("library previews: loading pinned CPU page detector")
            detector = DocLayNetPageDetector.from_huggingface(cache_dir=settings.model_cache_dir)
            return run_previews(
                store, candidates, runtime=OperationalStateStore(context.db.local_state_path),
                storage=storage, settings=settings, s3=s3, detector=detector, context=context,
            )
        finally:
            s3.close()
    finally:
        release_postgres_engine(engine)
