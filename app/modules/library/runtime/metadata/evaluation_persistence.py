"""Durable evaluation results and checkpoint completion."""

from __future__ import annotations

import json

from app.modules.library.metadata_contract import (
    CONTRACT_VERSION,
    metadata_contract_issues,
)
from app.modules.library.runtime.metadata.repository import (
    EVALUATION_PROMPT_VERSION,
    clear_evaluation_state,
)
from app.modules.library.runtime.models import Metadata
from app.modules.runtime_shared_utils import get_session

from .evaluation_classification import _resolve_classification_id
from .evaluation_patch import _apply_metadata_patch, _sanitize_schema_urls
from .evaluation_terms import _sync_auxiliary_terms_in_about
from .evaluation_types import Evaluation


def save_evaluation_result(
    md5: str,
    evaluation: Evaluation,
    *,
    model_name: str,
    dry_run: bool,
    log,
) -> None:
    if dry_run:
        log(f"Dry-run: would persist evaluation for {md5}")
        return
    with get_session() as session:
        metadata = session.get(Metadata, md5)
        if metadata:
            metadata.lib = bool(evaluation.applicable)
            metadata.lib_eval_method = f"{model_name}/{EVALUATION_PROMPT_VERSION}"
            if (
                evaluation.applicable
                and evaluation.library_ddc
                and evaluation.library_path
            ):
                metadata.classification_id = _resolve_classification_id(
                    session,
                    evaluation.library_ddc,
                    evaluation.library_path,
                )
            else:
                metadata.classification_id = None
            schema_org = (
                metadata.schema_org if isinstance(metadata.schema_org, dict) else {}
            )
            applied: list[str] = []
            if evaluation.metadata_patch:
                patch_payload = json.loads(
                    evaluation.metadata_patch.model_dump_json(
                        by_alias=True,
                        exclude_none=True,
                        ensure_ascii=False,
                    )
                )
                schema_org, patch_applied = _apply_metadata_patch(
                    schema_org, patch_payload
                )
                applied.extend(patch_applied)
            schema_org, classification_applied = _sync_auxiliary_terms_in_about(
                schema_org=schema_org,
                applicable=evaluation.applicable,
                ddc=evaluation.library_ddc,
                path=evaluation.library_path,
            )
            applied.extend(classification_applied)
            schema_org, url_applied = _sanitize_schema_urls(schema_org)
            if url_applied:
                applied.append("url")
            if applied:
                metadata.schema_org = schema_org
                log(f"Patched metadata for {md5}: {', '.join(applied)}")
            if issues := metadata_contract_issues(schema_org):
                codes = ", ".join(sorted({item["code"] for item in issues}))
                raise ValueError(
                    f"Refusing evaluation outside {CONTRACT_VERSION}: {codes}"
                )
            session.commit()
    clear_evaluation_state(md5)
