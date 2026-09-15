"""Library metadata evaluation response coverage."""

from __future__ import annotations

import json

import pytest

from app.gemini_model_pool import GeminiModelResponseError
from app.modules.library.runtime.metadata.evaluation_response import (
    _parse_evaluation_response,
)


def test_evaluation_response_requires_classification_only_when_applicable(
    evaluation_document,
) -> None:
    with pytest.raises(GeminiModelResponseError, match="classification"):
        _parse_evaluation_response(
            json.dumps({"applicable": True, "reason": "Tatar literary work"}),
            doc=evaluation_document(),
            config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
        )

    result = _parse_evaluation_response(
        json.dumps({"applicable": False, "reason": "not a library document"}),
        doc=evaluation_document(),
        config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
    )

    assert result.applicable is False
    assert result.reason == "not a library document"

    with pytest.raises(GeminiModelResponseError, match="reason"):
        _parse_evaluation_response(
            json.dumps({"applicable": False}),
            doc=evaluation_document(),
            config={"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}},
        )
