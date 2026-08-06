from __future__ import annotations

import pytest

from app.gemini_runtime import GeminiRuntimeManager, GeminiStopRequestedError


def test_gemini_wait_is_interruptible_for_graceful_stop() -> None:
    manager = GeminiRuntimeManager(
        object(),
        task_id="library.collection_validate",
        panel_id="library",
        should_stop=lambda: True,
    )

    with pytest.raises(GeminiStopRequestedError):
        manager._sleep_until(None)
