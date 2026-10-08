# Library owner lookup

Paths are relative to the repo root; all implementation rows below live in `app/modules/library/` unless an `app/` path is shown. Read the matching owner, then callers. Entry points assemble focused modules; internals must not import those entry points.

| Concern | Owners |
| --- | --- |
| Task registration and launch | `tasks.py`, `collection_tasks.py`, `runtime/run_*.py` |
| Normalization facade / durable writes | `normalization.py`, `app/repositories/normalization.py`, `app/catalog/personality_normalization.py` |
| Normalization matching / mentions / views | `normalization_rules.py`, `normalization_queries.py`, `normalization_views.py` |
| Canonicals / decisions / history / quality / suggestions | `normalization_canonicals.py`, `normalization_decisions.py`, `normalization_history.py`, `normalization_quality.py`, `normalization_suggestions.py` |
| Personality extraction and review | `personality_normalization.py`, `personality_normalization_prompt.py`, `personality_workbench.py`, `runtime/run_normalize_personalities.py`; [contract](../../../../docs/personality-normalization.md) |
| Publisher analysis / subprocess / review | `publisher_merge_contract.py`, `publisher_workbench.py`, `publisher_codex.py`, `publisher_merge_review.py`, `app/repositories/publisher_merges.py`; [contract](publisher-merges.md) |
| Non-PDF orchestration / detection / checkpoints | `non_pdf_extraction.py`, `non_pdf_formats.py`, `non_pdf_types.py`, `non_pdf_repository.py`; [document rules](documents.md) |
| Converters and rendering | Matching `non_pdf_*.py`; Google fallbacks: `google_doc_conversion.py`, `google_presentation_conversion.py` |
| Source storage / previews | `app/document_storage.py`, `preview_generation.py`, `preview_detection.py`, `preview_repository.py`, `catalog_preview_worker.py` |
| Metadata extraction and contract | `metadata_extraction.py`, `metadata_prompt.py`, `metadata_contract.py`, `djvu_slicing.py`; [rules](metadata.md) |
| Metadata evaluation | [focused owner lookup](evaluation-navigation.md) |
| Collections | `collection_detection.py`, `collection_validation.py`, `collection_catalog.py`, `runtime/run_collection_apply.py`; [rules](collections.md) |
| Static publishing export | `site_export.py`, `site_export_repository.py`, `runtime/run_site_export.py`; [contract](site-export.md) |
| Cleanup planning | `document_cleanup.py`, `document_cleanup_service.py`, `document_cleanup_repository.py`; execution belongs to Maintenance |
| CLI task composition | `app/cli/`; flow-owned execution: `runtime/run_normalize_personalities.py`, `runtime/run_prepare_document_cleanup.py`, `cleanup_cli.py` |

Legacy SQL/ORM assumptions need auditing against the migrated [catalog](../../../../docs/catalog-model.md). Validation policy and coverage limits: [verification](../../../../docs/verification.md).
