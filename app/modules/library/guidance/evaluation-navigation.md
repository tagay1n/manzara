# Metadata evaluation navigation

Policy: `app/modules/library/guidance/metadata.md` and `docs/gemini-runtime.md`. Paths are relative to the repository root. Read the matching owner rather than the whole workflow.

| Change | Implementation | Focused tests |
| --- | --- | --- |
| CLI, package imports | `app/modules/library/runtime/run_meta_evaluate.py` | `tests/test_meta_evaluate_entrypoint.py`, `tests/test_meta_evaluate_imports.py` |
| Batch loop, worker startup, stop coordination | `app/modules/library/runtime/metadata/evaluation.py` | `tests/test_library_metadata_evaluation_orchestration.py` |
| Request/response records | `app/modules/library/runtime/metadata/evaluation_types.py` | `tests/test_library_metadata_evaluation_response.py` |
| Catalog selection, early skips, classification examples | `app/modules/library/runtime/metadata/evaluation_selection.py`, `app/modules/library/runtime/metadata/repository.py` | `tests/test_library_metadata_evaluation_selection.py` |
| Worker-local sources, cache, PDF slices, prompt artifacts | `app/modules/library/runtime/metadata/evaluation_documents.py` | `tests/test_library_metadata_evaluation_documents.py` |
| Gemini request, model fallback, failure checkpoints | `app/modules/library/runtime/metadata/evaluation_request.py`, `app/modules/library/runtime/prompts/metadata_evaluation.py` | `tests/test_library_metadata_evaluation_request.py` |
| Response parsing, merged JSON-LD validation | `app/modules/library/runtime/metadata/evaluation_response.py` | `tests/test_library_metadata_evaluation_response.py` |
| Missing fields, metadata patch normalization and merge | `app/modules/library/runtime/metadata/evaluation_patch.py` | `tests/test_library_metadata_evaluation_response.py`, `tests/test_library_metadata_evaluation_persistence.py` |
| Managed genre/classification terms | `app/modules/library/runtime/metadata/evaluation_terms.py` | `tests/test_library_metadata_evaluation_persistence.py` |
| DDC/path normalization, classification lookup | `app/modules/library/runtime/metadata/evaluation_classification.py` | `tests/test_library_metadata_evaluation_response.py`, `tests/test_library_metadata_evaluation_persistence.py` |
| Evidence excerpts, text cleanup, response log formatting | `app/modules/library/runtime/metadata/evaluation_text.py` | `tests/test_library_metadata_evaluation_documents.py`, `tests/test_library_metadata_evaluation_request.py` |
| Durable result write, checkpoint completion | `app/modules/library/runtime/metadata/evaluation_persistence.py` | `tests/test_library_metadata_evaluation_persistence.py` |
| Worker outcomes, retryable deferrals, terminal failures | `app/modules/library/runtime/metadata/evaluation_worker.py` | `tests/test_library_metadata_evaluation_worker.py` |
| Progress counters, model attempts/successes | `app/modules/library/runtime/metadata/evaluation_progress.py` | `tests/test_library_metadata_evaluation_progress.py` |
| Shared deferrals, fatal errors, artifact state | `app/modules/library/runtime/metadata/evaluation_channel.py` | `tests/test_library_metadata_evaluation_worker.py` |

All imports use the repository package; the standalone script adds only the repository root to its import path. Shared config/session/path utilities live in `app/modules/runtime_shared_utils.py`. Each document loader owns its S3 clients. The request uses the shared Gemini manager and transport; persistence commits the validated result before clearing its retry checkpoint.

Fresh task factories live in the `evaluation_document` fixture in `tests/conftest.py`. Boundary and navigation checks: `tests/test_architecture_boundaries.py`.
