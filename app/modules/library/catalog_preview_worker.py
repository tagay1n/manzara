"""Bounded SQL discovery and sequential preview execution with durable claims."""

from contextlib import ExitStack
from dataclasses import replace
from time import monotonic

import psycopg2
from sqlalchemy.exc import SQLAlchemyError

from app.catalog.book_previews import PREVIEW_BATCH_SIZE
from app.catalog.contracts import CatalogConflict, CatalogNotFound, integer
from app.document_operation_lock import DocumentOperationBusy, check_document_operation, document_operation
from app.document_storage import verify_primary_document_object
from app.modules.library.preview_detection import PreviewModelError
from app.modules.library.preview_generation import _is_storage_fatal, process_book
from app.modules.library.previews import PREVIEW_RECIPE_VERSION
from app.runtime_config import config_integer
from app.task_runtime.logging import redact


def _progress(context, summary, *, phase="processing", force=False, **values):
    context.progress({"phase": phase, "current": summary["processed"], "total": summary["total"],
                      "ready": summary["ready"], "failed": summary["failed"],
                      "skipped": summary["skipped"], **values}, force=force)


def _process_preview(store, source, *, summary, context, runtime, storage, settings, s3, detector):
    """Return false only for a shared failure that must stop later batches."""
    md5 = source["md5"]
    actor = f"book-previews:{context.run_id}"
    request = None
    committed = False
    _progress(context, summary, md5=md5)
    try:
        with document_operation(store.engine, md5) as operation:
            lease_seconds = config_integer("previews", "lease_seconds", maximum=3600)
            request = store.claim(source, recipe=PREVIEW_RECIPE_VERSION, actor=actor,
                                  run_id=context.run_id, retry_known_failures=context.options.retry_known_failures,
                                  lease_seconds=lease_seconds, operation=operation)
            if request is None:
                summary["skipped"] += 1
                context.log(f"library previews: skip md5={md5} reason=cohort changed or already claimed")
                return True
            request_id = request["request_id"]
            context.log(f"library previews: start md5={md5} request_id={request_id}")
            runtime.put("library.preview_requests", request_id, {
                "md5": md5, "run_id": context.run_id, "error": None,
                "status": "processing", "claim_token": request["claim_token"],
            })
            # Use the configured lease rather than renewing for every CPU/read
            # boundary. Every upload still checks the owning session and source.
            renew_at = monotonic() + lease_seconds / 3

            def boundary(*, force=False):
                nonlocal renew_at
                check_document_operation(operation, probe=False)
                if force or monotonic() >= renew_at:
                    store.renew(source, request, lease_seconds=lease_seconds, operation=operation)
                    renew_at = monotonic() + lease_seconds / 3

            boundary()
            location = verify_primary_document_object(
                settings=storage, s3=s3, document_url=source["locator"], expected_size=source["size"],
            )
            result = process_book(
                md5, source_location=location, object_prefix=f"catalog/{request_id}/{request['claim_token']}/",
                settings=replace(settings, workspace=settings.workspace / str(request_id) / request["claim_token"]),
                source_s3=s3, target_s3=s3, page_detector=detector, boundary=boundary,
                publication_boundary=lambda: boundary(force=True), log=context.log,
            )
            store.finish(source, request, pages=list(result.pages),
                         source_page_count=result.source_page_count, actor=actor, operation=operation)
            committed = True
            summary["ready"] += 1
            for key in ("uploaded_objects", "reused_objects", "selected_pages", "inspected_pages",
                        "rejected_pages", "inference_seconds"):
                summary[key] += getattr(result, key)
            summary["downloaded_sources"] += int(result.downloaded_source)
            runtime.put("library.preview_requests", request_id, {
                "md5": md5, "run_id": context.run_id, "error": None, "status": "ready",
            })
            context.log(f"library previews: ready md5={md5} request_id={request_id} pages={result.selected_pages}")
    except DocumentOperationBusy as exc:
        summary["skipped"] += 1
        context.log(f"library previews: skip md5={md5} reason={exc}")
    except Exception as exc:
        if committed:
            raise
        error = redact(f"{type(exc).__name__}: {exc}")
        summary["failed"] += 1
        context.log(f"library previews: failed md5={md5} request_id={request['request_id'] if request else 'none'} error={error}")
        if request is not None:
            runtime.put("library.preview_requests", request["request_id"], {
                "md5": md5, "run_id": context.run_id, "error": error, "status": "failed",
            })
            # Never replay a write with an unknown commit acknowledgement.
            if not isinstance(exc, (psycopg2.Error, SQLAlchemyError)):
                try:
                    store.finish(source, request, pages=[], source_page_count=0, actor=actor, error=error)
                except (CatalogConflict, CatalogNotFound) as conflict:
                    context.log(f"library previews: failure checkpoint changed md5={md5} reason={conflict}")
        if isinstance(exc, (psycopg2.Error, SQLAlchemyError, PreviewModelError)) or _is_storage_fatal(exc):
            summary["error"] = error
            return False
    finally:
        summary["processed"] += 1
        _progress(context, summary)
    return True


def run_previews(store, *, context, load_resources):
    """One discovery query per batch; finish the current item before safe stop."""
    limit = context.options.limit
    if limit is not None:
        integer(limit, "limit")
    summary = {
        "kind": "library.catalog_preview_summary", "recipe_version": PREVIEW_RECIPE_VERSION,
        "total": 0, "processed": 0, "ready": 0, "failed": 0, "skipped": 0,
        "uploaded_objects": 0, "reused_objects": 0, "selected_pages": 0,
        "inspected_pages": 0, "rejected_pages": 0, "downloaded_sources": 0, "inference_seconds": 0.0,
    }
    after_md5 = None
    resources = None
    with ExitStack() as stack:
        while True:
            if context.should_stop() or (limit is not None and summary["total"] >= limit):
                break
            _progress(context, summary, phase="discovering", force=True)
            batch = store.candidate_batch(
                recipe=PREVIEW_RECIPE_VERSION,
                limit=min(PREVIEW_BATCH_SIZE, limit - summary["total"]) if limit is not None else PREVIEW_BATCH_SIZE,
                after_md5=after_md5, only_md5s=context.options.only_md5s,
                retry_known_failures=context.options.retry_known_failures,
            )
            context.log(f"library previews: discovered batch={len(batch)} recipe={PREVIEW_RECIPE_VERSION}")
            if not batch:
                break
            after_md5 = batch[-1]["md5"]
            summary["total"] += len(batch)
            if context.should_stop():
                break
            if resources is None:
                resources = stack.enter_context(load_resources())
                if resources is None or context.should_stop():
                    break
                summary["workspace_path"] = str(resources["settings"].workspace)
            for source in batch:
                if context.should_stop():
                    break
                if not _process_preview(store, source, summary=summary, context=context, **resources):
                    break
            if "error" in summary:
                break
    summary["stopped"] = context.should_stop()
    summary["outcome"] = "failed" if summary["failed"] else "stopped" if summary["stopped"] else "completed"
    _progress(context, summary, force=True)
    return summary
