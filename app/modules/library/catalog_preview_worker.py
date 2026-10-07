"""Render durable admin requests using the Library preview pipeline."""

from dataclasses import replace

from sqlalchemy import select

from app.catalog.contracts import integer
from app.document_storage import resolve_document_object_location
from app.modules.library.preview_generation import process_book
from app.modules.library.previews import PREVIEW_RECIPE_VERSION, preview_object_key


class PrefixStorage:
    """Each claim owns immutable object keys, including failed generations."""

    def __init__(self, storage, prefix, *, private):
        self.storage, self.prefix, self.private = storage, prefix, private

    def head_object(self, *, Bucket, Key):
        return self.storage.head_object(Bucket=Bucket, Key=self.prefix + Key)

    def upload_file(self, filename, bucket, key, **kwargs):
        extra = dict(kwargs.pop("ExtraArgs", {}))
        if self.private:
            extra["CacheControl"] = "private, no-store"
        return self.storage.upload_file(filename, bucket, self.prefix + key, ExtraArgs=extra, **kwargs)


def drain_preview_requests(repository, *, render, should_stop, actor, limit=None, on_progress=None):
    """Finish the current document before observing a stop; claims survive crashes."""
    if limit is not None:
        integer(limit, "limit")
    summary = {"kind": "library.catalog_preview_summary", "ready": 0, "failed": 0, "stopped": False}
    while not should_stop() and (limit is None or summary["ready"] + summary["failed"] < limit):
        request = repository.claim_preview(actor, lease_seconds=3600)
        if request is None:
            break
        try:
            result = render(request)
        except Exception as exc:
            repository.finish_preview(request["request_id"], request["claim_token"],
                pages=[], source_page_count=0, actor=actor, error=str(exc))
            raise
        repository.finish_preview(request["request_id"], request["claim_token"], actor=actor, **result)
        summary["failed" if result.get("error") else "ready"] += 1
        if on_progress is not None:
            on_progress(dict(summary))
    summary["stopped"] = bool(should_stop())
    return summary


class _Checkpoints:
    def __init__(self, repository, request):
        self.repository, self.request = repository, request
        self.latest = {}

    def start_attempt(self, *_args, **_kwargs):
        return {}

    def checkpoint(self, _md5, **values):
        self.repository.renew_preview(self.request["request_id"], self.request["claim_token"], lease_seconds=3600)
        self.latest = values


def render_catalog_preview(repository, request, *, settings, source_s3, target_s3,
                           page_detector, private_bucket, run_id, log):
    """Use only a verified primary location; private output never uses the public bucket."""
    if request["recipe"] != PREVIEW_RECIPE_VERSION:
        raise ValueError("Unsupported preview recipe; request the current Library recipe")
    locations = repository.table("locations")
    with repository.engine.connect() as conn:
        row = conn.execute(select(locations).where(locations.c.md5 == request["md5"],
            locations.c.provider == "s3", locations.c.purpose == "primary")).mappings().first()
    if row is None or row["verified_at"] is None or not resolve_document_object_location(
        document_url=row["locator"], encryption_key=settings.encryption_key, endpoint_url=settings.source_endpoint_url,
    ):
        raise ValueError("A verified primary PDF storage location is required")
    if request["private"] and not private_bucket:
        raise ValueError("Private preview storage is not configured")
    prefix = f"catalog/{request['request_id']}/{request['claim_token']}/"
    storage = PrefixStorage(target_s3, prefix, private=request["private"])
    checkpoints = _Checkpoints(repository, request)
    result = process_book({"md5": request["md5"], "document_url": row["locator"]},
        repository=checkpoints, settings=replace(settings,
            target_bucket=private_bucket if request["private"] else settings.target_bucket,
            workspace=settings.workspace / str(request["request_id"]) / request["claim_token"]),
        source_s3=source_s3, target_s3=storage, page_detector=page_detector, run_id=run_id, log=log)
    error = result.error or ("Preview generation did not complete" if result.status != "ready" else None)
    pages = [] if error else [{"role": page.role, "page_number": page.page_number,
        "small_key": prefix + preview_object_key(request["md5"], page.object_alias, "small"),
        "large_key": prefix + preview_object_key(request["md5"], page.object_alias, "large")}
        for page in checkpoints.latest.get("selected_pages", [])]
    return {"pages": pages, "source_page_count": checkpoints.latest.get("source_page_count") or 0, "error": error}
