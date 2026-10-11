# Metadata evaluation owners

Paths are relative to the repository root. Policy: [metadata contract](metadata.md) and [Gemini contract](../../../../docs/gemini-runtime.md).

| Concern | Owner |
| --- | --- |
| Interactive entry points | `app/modules/library/runtime/run_meta_evaluate.py`, `app/modules/library/runtime/run_metadata_extract.py` |
| Shared bounded publication loop, sequential processing, retries, progress, artifacts | `app/modules/library/runtime/run_metadata_processing.py` |
| Task-specific timeout/page defaults and editor | `app/metadata_settings.py`, `app/cli/settings.py`; private YAML `metadata.evaluation_response_timeout_seconds`, `evaluation_first_pages`, `evaluation_last_pages` |
| Aggregated candidate/evidence/classification discovery, source guards, inclusion and taxonomy writes | `app/catalog/metadata_processing.py`, `app/catalog/metadata_evaluation.sql`, shared envelope: `app/catalog/metadata_extraction.sql` |
| Single-request guarded metadata/decision/taxonomy/evidence saves | `app/catalog/metadata_save.py`, `app/catalog/metadata_save.sql` |
| Normalized bibliographic reads/writes, protections, reviewed contributions | `app/catalog/metadata_store.py` |
| Verified text/PDF/DjVu evidence | `app/modules/library/runtime/metadata/evaluation_evidence.py`, `app/modules/library/metadata_extraction.py` |
| Prompt | `app/modules/library/runtime/prompts/metadata_evaluation.py` |
| Parsing, patches, merged validation | `app/modules/library/runtime/metadata/evaluation_types.py`, `app/modules/library/runtime/metadata/evaluation_response.py`, `app/modules/library/runtime/metadata/evaluation_patch.py` |
| Managed terms, classification normalization, text | `app/modules/library/runtime/metadata/evaluation_terms.py`, `app/modules/library/runtime/metadata/evaluation_classification.py`, `app/modules/library/runtime/metadata/evaluation_text.py` |
