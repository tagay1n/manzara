"""Evaluation response validation and merged JSON-LD contract checks."""

from __future__ import annotations

import json
from typing import Any

from app.gemini_model_pool import GeminiModelResponseError
from app.modules.library.metadata_contract import (
    CONTRACT_VERSION,
    metadata_contract_issues,
)

from .evaluation_classification import _normalize_library_classification
from .evaluation_patch import (
    _apply_metadata_patch,
    _normalize_metadata_patch,
    _sanitize_schema_urls,
)
from .evaluation_terms import _sync_auxiliary_terms_in_about
from .evaluation_text import _clean_text
from .evaluation_types import Evaluation, EvaluationTask


def _parse_evaluation_response(
    raw_response: str,
    *,
    doc: EvaluationTask,
    config: dict,
) -> Evaluation:
    """Validate one response before it can become an evaluation result."""
    if not str(raw_response or "").strip():
        raise GeminiModelResponseError("metadata evaluation response is empty")
    try:
        evaluation = Evaluation.model_validate_json(raw_response)
    except Exception as exc:  # noqa: BLE001
        raise GeminiModelResponseError(
            f"metadata evaluation response is invalid: {exc}"
        ) from exc
    evaluation.metadata_patch = _normalize_metadata_patch(
        evaluation.metadata_patch,
        doc,
        config,
    )
    normalized_ddc, normalized_path = _normalize_library_classification(
        evaluation.library_ddc,
        evaluation.library_path,
        applicable=evaluation.applicable,
    )
    evaluation.library_ddc = normalized_ddc
    evaluation.library_path = normalized_path
    evaluation.reason = _clean_text(evaluation.reason, max_len=300)
    if not evaluation.reason:
        raise GeminiModelResponseError(
            "metadata evaluation has no usable decision reason"
        )
    if evaluation.applicable and (
        not evaluation.library_ddc or not evaluation.library_path
    ):
        raise GeminiModelResponseError(
            "applicable metadata evaluation has no usable classification"
        )
    merged = _schema_after_evaluation(doc.schema_org, evaluation)
    if issues := metadata_contract_issues(merged):
        issue_pairs = ", ".join(
            f"{code}@{path}"
            for code, path in sorted(
                {
                    (str(item.get("code") or "unknown"), str(item.get("path") or "$"))
                    for item in issues
                }
            )
        )
        raise GeminiModelResponseError(
            f"metadata evaluation violates {CONTRACT_VERSION}: {issue_pairs}"
        )
    return evaluation


def _schema_after_evaluation(
    existing: dict | str | None,
    evaluation: Evaluation,
) -> dict[str, Any]:
    schema_org = dict(existing) if isinstance(existing, dict) else {}
    if evaluation.metadata_patch:
        patch_payload = json.loads(
            evaluation.metadata_patch.model_dump_json(
                by_alias=True, exclude_none=True, ensure_ascii=False
            )
        )
        schema_org, _ = _apply_metadata_patch(schema_org, patch_payload)
    schema_org, _ = _sync_auxiliary_terms_in_about(
        schema_org=schema_org,
        applicable=evaluation.applicable,
        ddc=evaluation.library_ddc,
        path=evaluation.library_path,
    )
    schema_org, _ = _sanitize_schema_urls(schema_org)
    return schema_org
