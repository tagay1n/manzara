# Metadata evaluation owners

Paths are relative to the repository root. Policy: [metadata contract](metadata.md) and [Gemini contract](../../../../docs/gemini-runtime.md).

| Concern | Owner |
| --- | --- |
| Interactive entry points | `app/modules/library/runtime/run_meta_evaluate.py`, `app/modules/library/runtime/run_metadata_extract.py` |
| Fixed publication inventory, worker coordination, retries, progress, artifacts | `app/modules/library/runtime/run_metadata_processing.py` |
| Catalog snapshots, source guards, inclusion and taxonomy writes | `app/catalog/metadata_processing.py` |
| Normalized bibliographic reads/writes, protections, reviewed contributions | `app/catalog/metadata_store.py` |
| Verified text/PDF/DjVu evidence | `app/modules/library/runtime/metadata/evaluation_evidence.py`, `app/modules/library/metadata_extraction.py` |
| Prompt | `app/modules/library/runtime/prompts/metadata_evaluation.py` |
| Parsing, patches, merged validation | `app/modules/library/runtime/metadata/evaluation_types.py`, `app/modules/library/runtime/metadata/evaluation_response.py`, `app/modules/library/runtime/metadata/evaluation_patch.py` |
| Managed terms, classification normalization, text | `app/modules/library/runtime/metadata/evaluation_terms.py`, `app/modules/library/runtime/metadata/evaluation_classification.py`, `app/modules/library/runtime/metadata/evaluation_text.py` |

Commit audited catalog results before clearing retry checkpoints. The old ORM batch loop, environment-owned run ID, Yandex source fallback, automatic prompt dumps, and file-backed failure lists are removed. Tests and live execution require explicit owner authorization.
