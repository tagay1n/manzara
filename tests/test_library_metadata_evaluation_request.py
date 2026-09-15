"""Library metadata evaluation request coverage."""

from __future__ import annotations

import json
from app.modules.library.runtime.metadata import (
    evaluation_request as evaluation_request_module,
)
from app.modules.library.runtime.metadata.evaluation_documents import (
    EvaluationDocuments,
)


def test_request_advances_to_next_model_after_incomplete_response_and_logs_attempts(
    monkeypatch,
    capsys,
    evaluation_document,
) -> None:
    class _Manager:
        def run_with_key(self, *, model_name, call, run_id, max_attempts):  # noqa: ANN001
            assert max_attempts == 1
            return call("test-key", object())

    calls: list[str] = []
    prompts: list[str] = []

    def _request(**kwargs):  # noqa: ANN003
        model = kwargs["model_name"]
        calls.append(model)
        prompts.append("\n".join(part["text"] for part in kwargs["contents"]))
        if model == "model-first":
            return json.dumps({"applicable": True})
        return json.dumps(
            {
                "applicable": True,
                "reason": "Tatar literary work",
                "library_ddc": "894.36",
                "library_path": ["Literature", "Tatar literature"],
            }
        )

    monkeypatch.setattr(evaluation_request_module, "generate_structured_json", _request)
    monkeypatch.setattr(
        evaluation_request_module,
        "get_evaluation_attempted_models",
        lambda _md5: set(),
    )
    config = {"sup_langs": {"tt": {"codes": ["tt-Cyrl"]}}}
    documents = EvaluationDocuments(config=config, excerpt_chars=0, log=print)
    monkeypatch.setattr(documents, "prepare_pdf_slice", lambda _doc: None)
    monkeypatch.setattr(documents, "dump_prompt", lambda _md5, _prompt: None)
    result = evaluation_request_module.evaluate_document(
        evaluation_document(),
        config=config,
        documents=documents,
        manager=_Manager(),
        models=["model-first", "model-second"],
        known_classifications=[],
        dry_run=True,
        run_id=None,
        progress=None,
        log=print,
    )

    assert result.model_name == "model-second"
    assert calls == ["model-first", "model-second"]
    assert all(
        '"upstream_metadata": {"title": "Source title"}' in item for item in prompts
    )
    assert all("supporting evidence" in item for item in prompts)
    output = capsys.readouterr().out
    assert "Gemini request" in output
    assert f"md5={evaluation_document().md5}" in output
    assert "model=model-first" in output
    assert "model=model-second" in output
