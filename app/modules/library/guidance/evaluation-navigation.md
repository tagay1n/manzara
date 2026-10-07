# Metadata evaluation owners

All paths below are under `app/modules/library/runtime/`. Policy: [metadata rules](metadata.md) and [Gemini contract](../../../../docs/gemini-runtime.md).

| Concern | Owner |
| --- | --- |
| Script entry point | `run_meta_evaluate.py` |
| Batch loop, worker startup, stop coordination | `metadata/evaluation.py` |
| Records, catalog selection and sources | `metadata/evaluation_types.py`, `evaluation_selection.py`, `repository.py`, `evaluation_documents.py` |
| Gemini request and prompt | `metadata/evaluation_request.py`, `prompts/metadata_evaluation.py` |
| Parsing, merged validation, patches | `metadata/evaluation_response.py`, `evaluation_patch.py` |
| Managed terms, classification, evidence text | `metadata/evaluation_terms.py`, `evaluation_classification.py`, `evaluation_text.py` |
| Durable writes and worker outcomes | `metadata/evaluation_persistence.py`, `evaluation_worker.py` |
| Progress, deferrals, fatal errors, artifacts | `metadata/evaluation_progress.py`, `evaluation_channel.py` |

Shared config/session/path utilities: `app/modules/runtime_shared_utils.py`. Document loaders own their S3 clients; requests use the shared Gemini runtime. Commit validated results before clearing retry checkpoints. The legacy ORM/repository boundary needs catalog adaptation; no evaluation tests currently remain in this checkout.
