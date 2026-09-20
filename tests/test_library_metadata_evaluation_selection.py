"""Library metadata evaluation selection coverage."""

from __future__ import annotations

import inspect

from app.modules.library.runtime.metadata.repository import (
    _evaluation_state_allows_retry,
    fetch_docs_for_evaluation,
)


def test_evaluation_selection_reopens_only_incomplete_or_inconsistent_rows() -> None:
    source = inspect.getsource(fetch_docs_for_evaluation)

    assert "Metadata.lib.is_(None)" in source
    assert "Metadata.classification_id.is_(None)" in source
    assert "Metadata.lib_eval_method" not in source
    assert "_checkpoints().get" in source
    assert "LibraryUpstreamMetadata" in source
    assert "model_pool" in inspect.signature(fetch_docs_for_evaluation).parameters


def test_prompt_v4_reopens_prompt_v3_terminal_checkpoint() -> None:
    assert _evaluation_state_allows_retry(
        {"contract_version": "prompt.v3", "status": "terminal", "model_pool": ["m"]},
        ["m"],
    )
