"""Evaluation writes preserve metadata and clear checkpoints only after commit."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.modules.library.runtime.metadata import evaluation_persistence as persistence
from app.modules.library.runtime.metadata.evaluation_types import Evaluation
from app.modules.library.runtime.metadata.schema import MetadataPatch


@pytest.fixture
def result_store(monkeypatch):
    row = SimpleNamespace(
        schema_org={
            "@context": "https://schema.org",
            "@type": "Book",
            "name": "Existing title",
        },
        classification_id=4,
    )
    actions = []
    session = SimpleNamespace(
        get=lambda _model, _md5: row,
        commit=lambda: actions.append("commit"),
    )

    @contextmanager
    def get_session():
        yield session

    monkeypatch.setattr(persistence, "get_session", get_session)
    monkeypatch.setattr(
        persistence,
        "clear_evaluation_state",
        lambda md5: actions.append(("clear", md5)),
    )
    return SimpleNamespace(row=row, session=session, actions=actions)


def test_applicable_result_fills_gaps_and_commits_before_clearing_checkpoint(
    result_store, monkeypatch
):
    monkeypatch.setattr(persistence, "_resolve_classification_id", lambda *_args: 7)
    result = Evaluation(
        applicable=True,
        reason="Tatar literary work",
        library_ddc="894.36",
        library_path=["Literature", "Tatar literature"],
        metadata_patch=MetadataPatch(
            name="Replacement title",
            publisher={"@type": "Organization", "name": "Press"},
        ),
    )
    persistence.save_evaluation_result(
        "a" * 32, result, model_name="model", dry_run=False, log=lambda _message: None
    )

    assert result_store.row.schema_org["name"] == "Existing title"
    assert result_store.row.schema_org["publisher"]["name"] == "Press"
    assert result_store.row.classification_id == 7
    terms = {
        term["inDefinedTermSet"]["name"]: term
        for term in result_store.row.schema_org["about"]
    }
    assert terms["DDC"]["termCode"] == "894.36"
    assert terms["CategoryPath"]["name"] == "Literature > Tatar literature"
    assert result_store.actions == ["commit", ("clear", "a" * 32)]


def test_non_applicable_result_removes_classification(result_store):
    persistence.save_evaluation_result(
        "a" * 32,
        Evaluation.nonapplicable("Utility document"),
        model_name="model",
        dry_run=False,
        log=lambda _message: None,
    )

    assert result_store.row.lib is False
    assert result_store.row.classification_id is None
    assert result_store.row.schema_org["name"] == "Existing title"
    assert result_store.actions == ["commit", ("clear", "a" * 32)]


def test_invalid_merged_metadata_retains_retry_checkpoint(result_store):
    result_store.row.schema_org = {}
    with pytest.raises(ValueError, match="Refusing evaluation"):
        persistence.save_evaluation_result(
            "a" * 32,
            Evaluation.nonapplicable("Utility document"),
            model_name="model",
            dry_run=False,
            log=lambda _message: None,
        )
    assert result_store.actions == []


def test_failed_commit_retains_retry_checkpoint(result_store):
    def fail_commit():
        raise RuntimeError("database unavailable")

    result_store.session.commit = fail_commit
    with pytest.raises(RuntimeError, match="database unavailable"):
        persistence.save_evaluation_result(
            "a" * 32,
            Evaluation.nonapplicable("Utility document"),
            model_name="model",
            dry_run=False,
            log=lambda _message: None,
        )
    assert result_store.actions == []


def test_dry_run_does_not_write_or_clear_checkpoint(result_store):
    persistence.save_evaluation_result(
        "a" * 32,
        Evaluation.nonapplicable("Utility document"),
        model_name="model",
        dry_run=True,
        log=lambda _message: None,
    )
    assert result_store.actions == []
    assert result_store.row.classification_id == 4
