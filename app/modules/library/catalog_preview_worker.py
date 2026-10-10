"""Sequential preview execution with explicit logging and durable document claims."""

from dataclasses import replace

from sqlalchemy.exc import SQLAlchemyError

from app.catalog.contracts import CatalogConflict, CatalogNotFound
from app.document_operation_lock import (
    DocumentOperationBusy,
    check_document_operation,
    document_operation,
)
from app.document_storage import verify_primary_document_object
from app.modules.library.preview_detection import PreviewModelError
from app.modules.library.preview_generation import _is_storage_fatal, process_book
from app.modules.library.previews import PREVIEW_RECIPE_VERSION
from app.runtime_config import config_integer
from app.task_runtime.logging import redact


def run_previews(store, candidates, *, runtime, storage, settings, s3, detector, context):
    """Finish each claimed document before observing cooperative cancellation."""
    actor = f"book-previews:{context.run_id}"
    summary = {
        "kind": "library.catalog_preview_summary", "recipe_version": PREVIEW_RECIPE_VERSION,
        "workspace_path": str(settings.workspace), "total": len(candidates), "processed": 0,
        "ready": 0, "failed": 0, "skipped": 0, "uploaded_objects": 0, "reused_objects": 0,
        "selected_pages": 0, "inspected_pages": 0, "rejected_pages": 0,
        "downloaded_sources": 0, "inference_seconds": 0.0,
    }
    for source in candidates:
        if context.should_stop():
            break
        md5 = source["md5"]
        request = None
        committed = False
        context.progress({"phase": "processing", "current": summary["processed"],
                          "total": summary["total"], "md5": md5, "ready": summary["ready"],
                          "failed": summary["failed"]})
        try:
            with document_operation(store.engine, md5) as operation:
                check_document_operation(operation)
                request_id = store.prepare_request(
                    source, recipe=PREVIEW_RECIPE_VERSION, actor=actor, run_id=context.run_id,
                    retry_known_failures=context.options.retry_known_failures,
                )
                if request_id is None:
                    summary["skipped"] += 1
                    context.log(f"library previews: skip md5={md5} reason=cohort changed")
                    continue
                request = store.catalog.claim_preview(actor, lease_seconds=config_integer("previews", "lease_seconds", maximum=3600), request_id=request_id)
                if request is None:
                    summary["skipped"] += 1
                    context.log(f"library previews: skip md5={md5} reason=request already claimed")
                    continue
                context.log(f"library previews: start md5={md5} request_id={request_id}")
                runtime.put("library.preview_requests", request_id, {
                    "md5": md5, "run_id": context.run_id, "error": None,
                    "status": "processing", "claim_token": request["claim_token"],
                })

                def boundary():
                    check_document_operation(operation)
                    store.check_source(source, recipe=PREVIEW_RECIPE_VERSION)
                    store.catalog.renew_preview(request_id, request["claim_token"], lease_seconds=config_integer("previews", "lease_seconds", maximum=3600))

                boundary()
                location = verify_primary_document_object(
                    settings=storage, s3=s3, document_url=source["locator"], expected_size=source["size"],
                )
                result = process_book(
                    md5, source_location=location,
                    object_prefix=f"catalog/{request_id}/{request['claim_token']}/",
                    settings=replace(settings, workspace=settings.workspace / str(request_id) / request["claim_token"]),
                    source_s3=s3, target_s3=s3, page_detector=detector,
                    boundary=boundary, log=context.log,
                )
                check_document_operation(operation)
                store.finish(source, request, pages=list(result.pages),
                             source_page_count=result.source_page_count, actor=actor)
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
                # Failure finalization must not replace a newer claim or a prior success.
                try:
                    store.catalog.finish_preview(request["request_id"], request["claim_token"],
                        pages=[], source_page_count=0, actor=actor, error=error)
                except (CatalogConflict, CatalogNotFound) as conflict:
                    context.log(f"library previews: failure checkpoint changed md5={md5} reason={conflict}")
            if isinstance(exc, (SQLAlchemyError, PreviewModelError)) or _is_storage_fatal(exc):
                summary["error"] = error
                break
        finally:
            summary["processed"] += 1
            context.progress({"phase": "processing", "current": summary["processed"],
                              "total": summary["total"], "ready": summary["ready"],
                              "failed": summary["failed"], "skipped": summary["skipped"]})
    summary["stopped"] = context.should_stop()
    summary["outcome"] = "failed" if summary["failed"] else "stopped" if summary["stopped"] else "completed"
    context.progress({"phase": "processing", "current": summary["processed"], "total": summary["total"],
                      "ready": summary["ready"], "failed": summary["failed"], "skipped": summary["skipped"]}, force=True)
    return summary
