"""One document request with ordered model fallback and failure checkpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.gemini_model_pool import GeminiModelPoolResult, run_ordered_model_pool
from app.gemini_requests import generate_structured_json
from app.gemini_runtime import GeminiRuntimeManager
from app.modules.library.runtime.metadata.fields import extract_flat_fields
from app.modules.library.runtime.metadata.repository import (
    get_evaluation_attempted_models,
    record_evaluation_model_failure,
)
from app.modules.library.runtime.prompts.metadata_evaluation import (
    build_library_applicability_prompt,
)
from app.modules.library.upstream_metadata import sanitize_upstream_metadata

from .evaluation_documents import EvaluationDocuments
from .evaluation_patch import _collect_patch_fields
from .evaluation_progress import _EvaluationProgress
from .evaluation_response import _parse_evaluation_response
from .evaluation_text import _drop_none_values, _format_response_for_log
from .evaluation_types import Evaluation, EvaluationTask


def evaluate_document(
    doc: EvaluationTask,
    *,
    config: dict,
    documents: EvaluationDocuments,
    manager: GeminiRuntimeManager,
    models: list[str],
    known_classifications: list[dict[str, Any]],
    dry_run: bool,
    run_id: int | None,
    progress: _EvaluationProgress | None,
    log,
) -> GeminiModelPoolResult[Evaluation]:
    flattened_meta = extract_flat_fields(doc.schema_org)
    excerpt = documents.load_content_excerpt(doc)
    upstream_metadata = sanitize_upstream_metadata(doc.upstream_metadata)
    files: dict[str, str] = {}
    if excerpt is None and doc.mime_type == "application/pdf":
        if slice_path := documents.prepare_pdf_slice(doc):
            files[slice_path] = "application/pdf"
    payload = _drop_none_values(
        {
            # "md5": doc.md5,
            # "ya_path": doc.ya_path,
            "title": flattened_meta["title"],
            "author": flattened_meta["author"],
            "publisher": flattened_meta["publisher"],
            "genre": flattened_meta["genre"],
            # "language": doc.language,
            "publish_year": flattened_meta["publish_year"],
            "isbn": flattened_meta["isbn"],
            "page_count": doc.page_count,
            "upstream_metadata": upstream_metadata,
            "pdf_slice_attached": bool(files),
            "missing_fields": _collect_patch_fields(doc.schema_org),
            "known_classifications": [
                {"ddc": item["ddc"], "path": item["path"]}
                for item in known_classifications
            ],
        }
    )

    prompt = build_library_applicability_prompt(payload, content_excerpt=excerpt)
    documents.dump_prompt(doc.md5, prompt)

    def _call(model_name: str, api_key: str, _lease: Any) -> str:
        log(f"Gemini request md5={doc.md5} model={model_name}")
        if progress is not None:
            progress.record_model_attempt(model_name)
        raw_response = generate_structured_json(
            api_key=api_key,
            model_name=model_name,
            contents=prompt,
            response_schema=Evaluation,
            files={Path(path): mime for path, mime in files.items()},
            timeout_seconds=180,
        )
        log(
            f"Raw eval response for {doc.md5} model={model_name}:\n"
            f"{_format_response_for_log(raw_response)}"
        )
        return raw_response

    def _record_failure(model_name: str, kind: str, error: str) -> None:
        log(
            f"Evaluation model failed md5={doc.md5} model={model_name} "
            f"kind={kind} error={error}"
        )
        if not dry_run:
            record_evaluation_model_failure(
                doc.md5,
                model_name=model_name,
                kind=kind,
                error=error,
                models=models,
                run_id=run_id,
            )

    return run_ordered_model_pool(
        manager=manager,
        models=models,
        request=_call,
        parse=lambda raw: _parse_evaluation_response(
            raw,
            doc=doc,
            config=config,
        ),
        record_failure=_record_failure,
        run_id=run_id,
        already_attempted=get_evaluation_attempted_models(doc.md5),
    )
