from __future__ import annotations

import pytest
import threading
import time

from app.gemini_config import GeminiKey
from app.gemini_workers import resolve_gemini_workers, validate_gemini_workers


def _keys() -> list[GeminiKey]:
    return [
        GeminiKey(f"account-{index}", f"key-{index}", "secret", "masked")
        for index in range(1, 4)
    ]


def test_worker_resolution_uses_cli_then_environment(monkeypatch) -> None:
    monkeypatch.setattr("app.gemini_workers.load_gemini_keys", _keys)
    monkeypatch.setenv("MANZARA_GEMINI_WORKERS", "2")
    assert resolve_gemini_workers() == 2
    assert resolve_gemini_workers(3) == 3


@pytest.mark.parametrize("value", [True, 1.5, 0, 4])
def test_worker_validation_is_strict_and_account_bounded(monkeypatch, value) -> None:  # noqa: ANN001
    monkeypatch.setattr("app.gemini_workers.load_gemini_keys", _keys)
    with pytest.raises(ValueError):
        validate_gemini_workers(value)


def test_normalization_preserves_order_while_gemini_calls_run_concurrently(monkeypatch) -> None:
    from app.modules.library import normalization_suggestions as normalization

    items = [
        {
            "raw_name": f"Publisher {index}",
            "normalized_name": f"publisher {index}",
            "docs_count": 2,
            "mentions_count": 2,
            "marker_count": 1,
            "queue_status": "unreviewed",
        }
        for index in range(3)
    ]
    monkeypatch.setattr(
        normalization,
        "get_review_queue",
        lambda *_args, **_kwargs: {"items": items},
    )
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def suggest(**_kwargs):  # noqa: ANN003
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return None

    monkeypatch.setattr(normalization, "_gemini_suggest", suggest)

    class Db:
        def list_normalization_canonicals(self, _entity_type):  # noqa: ANN001
            return []

    result = normalization._heuristic_suggestions(
        Db(), "publisher", limit=3, use_gemini=True, manager=object(), workers=2
    )

    assert maximum_active == 2
    assert [item["raw_name"] for item in result] == [item["raw_name"] for item in items]
