# Library navigation

Paths below are relative to the repository root. Read only the row matching the task, then its callers if needed. Public entry points assemble focused owners; implementation modules must not import those entry points.

## Normalization

Public API: `app/modules/library/normalization.py`. HTTP wiring: `app/library_normalization_routes.py`, `app/dependencies.py`. Durable operations: `app/repositories/normalization.py`.

| Change | Implementation | Focused tests |
| --- | --- | --- |
| Entity fields, name matching, confidence | `app/modules/library/normalization_rules.py` | `tests/test_library_normalization.py`, `tests/test_gemini_workers.py` |
| Catalog mentions, source snapshots, evidence | `app/modules/library/normalization_queries.py` | `tests/test_api_library_normalization.py` |
| Dashboard, review queue enrichment | `app/modules/library/normalization_views.py` | `tests/test_api_library_normalization.py` |
| Canonical creation, groups, rename, merge | `app/modules/library/normalization_canonicals.py` | `tests/test_library_normalization.py`, `tests/test_api_library_normalization.py` |
| Alias link/reject, bulk decisions, dismissal | `app/modules/library/normalization_decisions.py` | `tests/test_api_library_normalization.py` |
| Audit history, undo | `app/modules/library/normalization_history.py` | `tests/test_library_normalization.py`, `tests/test_api_library_normalization.py` |
| Coverage, merge recommendations | `app/modules/library/normalization_quality.py` | `tests/test_api_library_normalization.py` |
| AI suggestions, refresh workers | `app/modules/library/normalization_suggestions.py` | `tests/test_gemini_workers.py`, `tests/test_gemini_config.py` |
| Canonical personality extraction, structured response, workbench | `app/modules/library/personality_normalization.py`, `app/modules/library/personality_normalization_prompt.py`, `app/modules/library/personality_workbench.py`, `app/modules/library/runtime/run_normalize_personalities.py` | `tests/test_library_personality_normalization.py`, `tests/test_library_personality_outcomes.py`, `tests/test_library_personality_workbench.py`, `tests/frontend/test_personality_decisions.mjs` |

## Documents and conversion

Policy: `app/modules/library/guidance/documents.md`.

| Change | Implementation | Focused tests |
| --- | --- | --- |
| Extraction preparation, local/Drive fallback orchestration | `app/modules/library/non_pdf_extraction.py` | `tests/test_library_non_pdf_formats.py`, `tests/test_library_non_pdf_converters.py` |
| Extraction records, errors, recipe version | `app/modules/library/non_pdf_types.py` | `tests/test_library_non_pdf_contracts.py` |
| Byte detection, legacy text decoding | `app/modules/library/non_pdf_formats.py` | `tests/test_library_non_pdf_formats.py` |
| Native PPTX text/tables, slide visual inspection and deferral | `app/modules/library/non_pdf_pptx.py` | `tests/test_library_pptx.py`, `tests/test_library_non_pdf_runtime.py` |
| Process timeouts, LibreOffice, DOCX ZIP, FB2 | `app/modules/library/non_pdf_converters.py` | `tests/test_library_non_pdf_converters.py`, `tests/test_library_non_pdf_media.py` |
| Embedded images, conversion, URL rewriting | `app/modules/library/non_pdf_media.py` | `tests/test_library_non_pdf_media.py` |
| Pandoc AST, figures, captions, tables | `app/modules/library/non_pdf_pandoc.py` | `tests/test_library_non_pdf_rendering.py` |
| Final Markdown, publication validation report | `app/modules/library/non_pdf_rendering.py` | `tests/test_library_non_pdf_rendering.py` |
| Publication, progress, corruption outcomes | `app/modules/library/runtime/run_extract_non_pdf.py` | `tests/test_library_non_pdf_runtime.py` |
| Candidate selection, attempt checkpoints | `app/modules/library/non_pdf_repository.py` | `tests/test_library_non_pdf_repository.py` |
| Google Drive DOCX fallback | `app/modules/library/google_doc_conversion.py` | `tests/test_library_non_pdf_converters.py` |

| Other document work | Implementation | Focused tests |
| --- | --- | --- |
| Source cache and storage | `app/document_storage.py` | `tests/test_document_storage.py` |
| Preview selection and publication | `app/modules/library/preview_generation.py`, `app/modules/library/runtime/run_generate_book_previews.py`, `app/modules/library/preview_repository.py` | `tests/test_library_preview_generation.py`, `tests/test_library_previews.py` |

## Metadata, collections, publishing

| Change | Policy | Implementation | Focused tests |
| --- | --- | --- | --- |
| Metadata extraction | `app/modules/library/guidance/metadata.md` | `app/modules/library/metadata_extraction.py`, `app/modules/library/metadata_prompt.py` | `tests/test_library_metadata_extraction.py`, `tests/test_library_metadata_extraction_runtime.py` |
| JSON-LD extraction contract | `app/modules/library/guidance/metadata.md` | `app/modules/library/metadata_contract.py` | `tests/test_library_metadata_contract.py` |
| Applicability, classification, gap filling | `app/modules/library/guidance/metadata.md` | Focused lookup: `app/modules/library/guidance/evaluation-navigation.md` | Select the evaluation lookup's focused tests |
| Collection detection | `app/modules/library/guidance/collections.md` | `app/modules/library/collection_detection.py` | `tests/test_library_collection_detection.py` |
| Collection validation and review | `app/modules/library/guidance/collections.md` | `app/modules/library/collection_validation.py`, `app/modules/library/collection_catalog.py` | `tests/test_library_collection_review.py` |
| Collection apply and merge | `app/modules/library/guidance/collections.md` | `app/modules/library/runtime/run_collection_apply.py` | `tests/test_library_collection_merge.py` |
| Static publishing | `app/modules/library/guidance/site-export.md` | `app/modules/library/site_export.py`, `app/modules/library/site_export_repository.py`, `app/modules/library/runtime/run_site_export.py` | `tests/test_library_site_export.py` |

Module boundaries and navigation paths: `tests/test_architecture_boundaries.py`. Public wiring: `tests/test_wiring_contracts.py`. Commands and full verification requirements: `docs/verification.md`.

Library HTTP contracts are split by domain: `tests/test_api_library_classification.py`, `tests/test_api_library_entities.py`, `tests/test_api_library_collections.py`, `tests/test_api_library_documents.py`, and `tests/test_api_library_normalization.py`. API fakes use the `override_operations` fixture in `tests/conftest.py`; replace the relevant service instead of patching business functions on `app.main`.
