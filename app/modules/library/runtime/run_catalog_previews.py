"""Drain durable catalog preview requests at safe document boundaries."""

import argparse
import signal

from boto3 import Session
from botocore.config import Config
from sqlalchemy import text
from app.postgres_engine import acquire_postgres_engine, release_postgres_engine

from app.catalog.repository import CatalogRepository
from app.document_storage import load_document_storage_settings
from app.modules.library.catalog_preview_worker import drain_preview_requests, render_catalog_preview
from app.modules.library.preview_detection import DocLayNetPageDetector
from app.modules.library.preview_runtime import _resolved_settings, _run_id
from app.modules.library.previews import PREVIEW_RECIPE_VERSION
from app.run_artifact_channel import emit_run_artifact
from app.runtime_config import load_runtime_config
from app.settings import load_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    run_id = _run_id()
    configuration = load_runtime_config()
    settings, credentials = _resolved_settings(configuration, run_id=run_id)
    storage = load_document_storage_settings(configuration)
    app_settings = load_settings()
    engine = acquire_postgres_engine(app_settings.database_url, schema=app_settings.database_schema)
    stop = {"requested": False}

    def request_stop(_signum, _frame):
        stop["requested"] = True
        print("Catalog previews: stop requested; finishing current document", flush=True)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    summary = {"kind": "library.catalog_preview_summary", "ready": 0, "failed": 0, "stopped": False}
    try:
        client = Session().client("s3", endpoint_url=settings.source_endpoint_url,
            region_name=settings.source_region_name, aws_access_key_id=credentials["source_access_key_id"],
            aws_secret_access_key=credentials["source_secret_access_key"],
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
        detector = DocLayNetPageDetector.from_huggingface(cache_dir=settings.model_cache_dir)
        repository = CatalogRepository(engine, schema=app_settings.database_schema)
        with engine.connect() as conn:
            pending = conn.execute(text("""SELECT d.md5 FROM catalog_documents d
                JOIN catalog_publications p USING(publication_id)
                WHERE p.inclusion='included' AND d.mime_type='application/pdf' AND NOT d.restricted
                AND NOT EXISTS(SELECT 1 FROM catalog_preview_requests r WHERE r.md5=d.md5 AND r.recipe=:recipe AND r.status='ready')
                ORDER BY d.md5"""), {"recipe": PREVIEW_RECIPE_VERSION}).scalars().all()
        for md5 in pending:
            if stop["requested"]:
                break
            repository.request_preview(md5, actor=f"preview-worker:{run_id}",
                idempotency_key=f"recipe:{PREVIEW_RECIPE_VERSION}", recipe=PREVIEW_RECIPE_VERSION)

        def render(request):
            return render_catalog_preview(repository, request, settings=settings, source_s3=client,
                target_s3=client, page_detector=detector, private_bucket=storage.private_bucket,
                run_id=run_id, log=lambda message: print(message, flush=True))

        def progress(value):
            summary.update(value)
            emit_run_artifact(dict(summary))

        summary = drain_preview_requests(repository, render=render,
            should_stop=lambda: stop["requested"], actor=f"catalog-preview-worker:{run_id}",
            limit=args.limit, on_progress=progress)
        return 0
    except Exception as exc:
        summary["error"] = str(exc)
        summary["failed"] += 1
        raise
    finally:
        emit_run_artifact(summary)
        release_postgres_engine(engine)


if __name__ == "__main__":
    raise SystemExit(main())
