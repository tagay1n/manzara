"""Catalog preview worker preserves generation and access boundaries."""

from types import SimpleNamespace

import pytest

from app.modules.library.catalog_preview_worker import PrefixStorage, drain_preview_requests


def test_catalog_preview_worker_is_available_to_task_runtime():
    from app.modules.library.tasks import library_task_definitions

    task = next(row for row in library_task_definitions() if row["task_id"] == "library.catalog_preview_requests")
    assert "run_catalog_previews" in task["command"]["value"]


def test_private_storage_prefix_and_cache_headers():
    calls = []
    storage = SimpleNamespace(upload_file=lambda *args, **kwargs: calls.append((args, kwargs)))
    wrapper = PrefixStorage(storage, "catalog/4/token/", private=True)
    wrapper.upload_file("a.webp", "private", "md5/1s.webp", ExtraArgs={"CacheControl": "public", "ContentType": "image/webp"})
    assert calls[0][0][2] == "catalog/4/token/md5/1s.webp"
    assert calls[0][1]["ExtraArgs"]["CacheControl"] == "private, no-store"


def test_worker_finishes_current_document_before_stopping():
    events = []
    request = {"request_id": 1, "claim_token": "token", "md5": "a" * 32}
    repository = SimpleNamespace(
        claim_preview=lambda *_args, **_kwargs: request,
        finish_preview=lambda *args, **kwargs: events.append((args, kwargs)),
    )
    state = {"stop": False}

    def render(_request):
        state["stop"] = True
        return {"pages": [], "source_page_count": 1}

    summary = drain_preview_requests(repository, render=render, should_stop=lambda: state["stop"], actor="worker")
    assert len(events) == 1
    assert summary["ready"] == 1 and summary["stopped"] is True


def test_worker_persists_actionable_failure_before_propagating_setup_error():
    events = []
    repository = SimpleNamespace(
        claim_preview=lambda *_args, **_kwargs: {"request_id": 1, "claim_token": "token", "md5": "a" * 32},
        finish_preview=lambda *args, **kwargs: events.append(kwargs),
    )

    def render(_request):
        raise RuntimeError("Preview storage is unavailable")

    with pytest.raises(RuntimeError, match="storage"):
        drain_preview_requests(repository, render=render, should_stop=lambda: False, actor="worker")
    assert events[0]["error"] == "Preview storage is unavailable"
