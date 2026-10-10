# Library owner lookup

Paths are relative to the repo root; all implementation rows below live in `app/modules/library/` unless an `app/` path is shown. Read the matching owner, then callers. Entry points assemble focused modules; internals must not import those entry points.

| Concern | Owners |
| --- | --- |
| Task registration and launch | `tasks.py`, `collection_tasks.py`, `runtime/run_*.py` |
| Normalization source reads / durable writes | `app/repositories/normalization.py`, `app/catalog/personality_normalization.py` |
| Personality extraction and checkpoints | `personality_normalization.py`, `personality_normalization_prompt.py`, `runtime/run_normalize_personalities.py`; [contract](../../../../docs/personality-normalization.md) |
| Publisher analysis / subprocess / proposals | `runtime/run_suggest_publisher_merges.py`, `publisher_merge_contract.py`, `publisher_codex.py`; catalog: `app/catalog/publisher_analysis.py`; database composition: `app/repositories/publisher_merges.py`; [contract](publisher-merges.md) |
| Non-PDF orchestration / detection / checkpoints | `non_pdf_extraction.py`, `non_pdf_formats.py`, `non_pdf_types.py`, `non_pdf_repository.py`; durable catalog commands: `app/catalog/non_pdf.py`; [document rules](documents.md) |
| Converters and rendering | Matching `non_pdf_*.py`; Google fallbacks: `google_doc_conversion.py`, `google_presentation_conversion.py` |
| Source storage / previews | `app/catalog/book_previews.py`, `app/catalog/previews.py`, `runtime/run_generate_book_previews.py`, `app/document_storage.py`, `preview_generation.py`, `preview_detection.py`, `catalog_preview_worker.py` |
| Metadata extraction and contract | `runtime/run_metadata_processing.py`, `app/catalog/metadata_processing.py`, `app/catalog/metadata_store.py`, `metadata_extraction.py`, `metadata_prompt.py`, `metadata_contract.py`, `djvu_slicing.py`; [rules](metadata.md) |
| Metadata evaluation | [focused owner lookup](evaluation-navigation.md) |
| Collections | `collection_detection.py`, `runtime/run_collection_detect.py`; catalog: `app/catalog/collection_discovery.py`; [rules](collections.md) |
| Static publishing export | `site_export.py`, `site_export_repository.py`, `runtime/run_site_export.py`; [contract](site-export.md) |
| Cleanup planning | `document_cleanup.py`, `document_cleanup_service.py`, shared `app/repositories/document_cleanup.py`; execution belongs to Maintenance |
| CLI / scheduled composition | `app/cli/`, `scripts/run_daily_maintenance.py`; flow-owned execution: `runtime/run_normalize_personalities.py`, `runtime/run_prepare_document_cleanup.py`, `cleanup_cli.py` |
