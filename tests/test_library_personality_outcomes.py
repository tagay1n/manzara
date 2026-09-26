"""Integrated personality decisions and bounded shared-queue retries."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.gemini_model_pool import GeminiModelPoolOperationalError
from app.modules.library.personality_normalization import PersonComponents, PersonalityCandidate, PersonalityResponse
from app.modules.library.runtime import run_normalize_personalities as runner


def response(outcome="normalized", reason=None, **components):
    return json.dumps({"outcome": outcome, "reason": reason,
                       **{field: None for field in PersonComponents.model_fields}, **components})


class Db:
    def __init__(self):
        self.checkpoints = {}
        self.saved = []
        self.progress = []

    def list_personality_checkpoints(self):
        return list(self.checkpoints.values())

    def get_personality_checkpoint(self, name):
        return self.checkpoints.get(name)

    def save_personality_checkpoint(self, **values):
        self.saved.append(values)
        self.checkpoints[values["raw_name"]] = values

    def persist_personality_normalization(self, **values):
        self.saved.append(values)
        self.checkpoints[values["raw_name"]] = {**values, "state": "succeeded"}
        return {"canonical_id": 1}

    def publish_run_progress(self, **values):
        self.progress.append(values["progress"])


class Manager:
    def __init__(self, *args, **kwargs):
        pass

    def run_with_key(self, *, model_name, call, **kwargs):
        return call("fake-key", None)

    def _sleep_until(self, when):
        pass


@pytest.mark.parametrize("outcome", ["not_person", "unusable"])
def test_negative_decision_persists_without_canonical_or_model_fallback_and_resumes(monkeypatch, outcome):
    monkeypatch.setattr(runner, "GeminiRuntimeManager", Manager)
    db = Db()
    names = [PersonalityCandidate("Raw source", 2, 3, ("author",), ("tt",))]
    calls = []

    def request(**kwargs):
        assert kwargs["response_schema"] is PersonalityResponse
        calls.append(kwargs["model_name"])
        return response(outcome, "Source identifies an institution" if outcome == "not_person" else "Corrupted source")

    summary = runner.run_personality_normalization(db=db, models=["first", "second"], run_id=1,
        should_stop=lambda: False, candidates=names, request_json=request)
    assert calls == ["first"]
    assert summary[outcome] == 1
    assert summary.get("succeeded", 0) == 0
    assert summary["processed"] == 1
    stored = db.get_personality_checkpoint(names[0].raw_name)
    assert stored["state"] == outcome
    assert stored["failure_context"]
    assert stored["document_count"] == 2
    assert stored["source_roles"] == ["author"]
    assert stored.get("canonical_id") is None
    assert stored["completed"] is True
    assert stored["attempted_models"]["first"]["document_languages"] == ["tt"]
    again = runner.run_personality_normalization(db=db, models=["first", "second"], run_id=None,
        should_stop=lambda: False, candidates=names, request_json=request)
    assert again["skipped"] == 1
    assert calls == ["first"]
    changed = PersonalityCandidate("Raw source", 2, 3, ("author",), ("en",))
    assert runner._eligible_candidates([changed], db.list_personality_checkpoints())[0] == [changed]


def test_invalid_negative_response_falls_back_to_valid_normalization(monkeypatch):
    monkeypatch.setattr(runner, "GeminiRuntimeManager", Manager)
    db = Db()
    calls = []

    def request(**kwargs):
        calls.append(kwargs["model_name"])
        if len(calls) == 1:
            return response("not_person", "Organization", name_full="Contradiction")
        return response(name_full="Person", father_name_initial="Kh.")

    summary = runner.run_personality_normalization(db=db, models=["first", "second"], run_id=None,
        should_stop=lambda: False, candidates=[PersonalityCandidate("Raw", 1, 1, ("author",))], request_json=request)
    assert calls == ["first", "second"]
    assert summary["succeeded"] == 1


@pytest.mark.parametrize("workers", [1, 2])
def test_transient_item_moves_to_tail_once_and_unique_progress_remains_correct(monkeypatch, workers):
    monkeypatch.setattr(runner, "GeminiRuntimeManager", Manager)
    db = Db()
    calls = []
    failures = {"A": 0}

    def pool(**kwargs):
        assert kwargs["yield_on_transient"] is True
        raw = kwargs["request"]("first", "key", None)
        name = json.loads(raw)["name_full"]
        calls.append(name)
        if name == "A" and failures["A"] == 0:
            failures["A"] += 1
            raise GeminiModelPoolOperationalError("503 high demand", retryable=True)
        return SimpleNamespace(model_name="first", value=kwargs["parse"](raw))

    def request(**kwargs):
        name = kwargs["contents"][0].split("<raw_name>")[1].split("</raw_name>")[0]
        return response(name_full=name)

    monkeypatch.setattr(runner, "run_ordered_model_pool", pool)
    summary = runner.run_personality_normalization(db=db, models=["first"], run_id=1,
        should_stop=lambda: False, candidates=[PersonalityCandidate(name, 1, 1, ("author",)) for name in ("A", "B", "C")],
        request_json=request, workers=workers)
    assert calls.count("A") == 2
    assert calls.index("B") < len(calls) - 1
    assert calls.index("C") < len(calls) - 1
    assert summary["processed"] == 3
    assert summary["succeeded"] == 3
    assert summary.get("deferred", 0) == 0
    assert summary["model_attempts"] == {"first": 4}
    assert all(item["processed"] <= 3 for item in db.progress)
    assert [item["processed"] for item in db.progress] == sorted(item["processed"] for item in db.progress)


def test_repeated_transient_failure_is_durably_deferred_after_two_turns(monkeypatch):
    monkeypatch.setattr(runner, "GeminiRuntimeManager", Manager)
    db = Db()
    calls = []

    def pool(**kwargs):
        calls.append(1)
        raise GeminiModelPoolOperationalError("504 service deadline", retryable=True)

    monkeypatch.setattr(runner, "run_ordered_model_pool", pool)
    summary = runner.run_personality_normalization(db=db, models=["first"], run_id=None,
        should_stop=lambda: False, candidates=[PersonalityCandidate("Raw", 1, 1, ("author",))])
    assert len(calls) == 2
    assert summary["processed"] == summary["deferred"] == 1
    assert db.checkpoints["Raw"]["retryable"] is True
    assert "504" in db.checkpoints["Raw"]["failure_context"]


def test_legacy_successes_stay_closed_and_only_all_null_failures_reopen():
    names = [PersonalityCandidate(name, 1, 1, ("author",)) for name in ("Done", "All null", "Bad JSON")]
    checkpoints = [{"raw_name": item.raw_name, "source_fingerprint": runner.personality_source_fingerprint(item),
        "schema_version": "person-components-v1", "prompt_version": "personality-components-v8",
        "state": "succeeded" if index == 0 else "failed", "retryable": False,
        "attempted_models": {"first": {"kind": "response", "error": (
            "at least one usable surname or personal-name component is required" if index == 1 else "invalid JSON")}}
    } for index, item in enumerate(names)]
    selected, skipped = runner._eligible_candidates(names, checkpoints)
    assert selected == [names[1]]
    assert skipped == 2
    attempts = runner._checkpoint_attempts_for_candidate(checkpoints[1], names[1])
    assert runner._excluded_models(attempts) == set()


def test_stop_during_service_pause_keeps_retry_checkpoint_and_does_not_start_next_person(monkeypatch):
    from datetime import datetime, timezone
    from app.gemini_runtime import GeminiStopRequestedError

    class StopManager(Manager):
        def _sleep_until(self, when):
            raise GeminiStopRequestedError("Stopped during pause")

    monkeypatch.setattr(runner, "GeminiRuntimeManager", StopManager)
    calls = []
    db = Db()

    def pool(**kwargs):
        calls.append(1)
        raise GeminiModelPoolOperationalError("503 high demand", retryable=True,
                                             retry_at=datetime.now(timezone.utc))

    monkeypatch.setattr(runner, "run_ordered_model_pool", pool)
    summary = runner.run_personality_normalization(db=db, models=["first"], run_id=1,
        should_stop=lambda: False, candidates=[PersonalityCandidate(name, 1, 1, ("author",)) for name in ("A", "B")])
    assert len(calls) == 1
    assert summary["outcome"] == "stopped"
    assert summary["deferred"] == 1
    assert db.checkpoints["A"]["retryable"] is True
    assert "B" not in db.checkpoints


def test_unavailable_pool_does_not_cycle_untouched_people(monkeypatch):
    from app.gemini_model_pool import GeminiModelPoolUnavailableError
    monkeypatch.setattr(runner, "GeminiRuntimeManager", Manager)
    db = Db()
    calls = []

    def pool(**kwargs):
        calls.append(1)
        raise GeminiModelPoolUnavailableError(["first"])

    monkeypatch.setattr(runner, "run_ordered_model_pool", pool)
    summary = runner.run_personality_normalization(db=db, models=["first"], run_id=None,
        should_stop=lambda: False, candidates=[PersonalityCandidate(name, 1, 1, ("author",)) for name in ("A", "B", "C")], workers=1)
    assert calls == [1]
    assert summary["outcome"] == "deferred"
    assert summary["processed"] == summary["deferred"] == 1
    assert db.checkpoints["A"]["retryable"] is True


def test_explicit_retry_reopens_negative_decision_without_excluding_deciding_model(monkeypatch):
    monkeypatch.setattr(runner, "GeminiRuntimeManager", Manager)
    db = Db()
    candidate = PersonalityCandidate("Corrected source", 1, 1, ("author",))
    db.checkpoints[candidate.raw_name] = {
        "raw_name": candidate.raw_name, "source_fingerprint": runner.personality_source_fingerprint(candidate),
        "prompt_version": runner.PERSONALITY_NORMALIZATION_PROMPT_VERSION, "schema_version": runner.SCHEMA_VERSION,
        "state": "retry_requested", "retryable": True,
        "attempted_models": {"first": {"kind": "decision", "outcome": "unusable", "reason": "Ambiguous"}},
    }
    summary = runner.run_personality_normalization(db=db, models=["first"], run_id=None, should_stop=lambda: False,
        candidates=[candidate], request_json=lambda **kwargs: response(name_full="Corrected"))
    assert summary["succeeded"] == 1


def test_prompt_examples_are_complete_under_the_outcome_contract():
    from app.modules.library.personality_normalization_prompt import (
        build_personality_normalization_prompt, DISABLED_PERSONALITY_NORMALIZATION_EXAMPLES,
    )
    prompt = build_personality_normalization_prompt("Example Name")
    for line in prompt.splitlines():
        if line.startswith("Output: "):
            assert PersonalityResponse.model_validate_json(line[8:]).outcome == "normalized"
    for item in DISABLED_PERSONALITY_NORMALIZATION_EXAMPLES:
        PersonalityResponse.model_validate_json(item["output"])
    assert "not_person" in prompt and "unusable" in prompt


def test_explicit_retry_and_changed_input_release_old_content_exclusions():
    candidate = PersonalityCandidate("Raw", 2, 2, ("author",))
    checkpoint = {"raw_name": "Raw", "source_fingerprint": "old-input", "state": "unusable",
        "schema_version": runner.SCHEMA_VERSION, "prompt_version": runner.PERSONALITY_NORMALIZATION_PROMPT_VERSION,
        "attempted_models": {"first": {"kind": "response", "error": "Invalid JSON"},
                             "second": {"kind": "decision", "outcome": "unusable", "reason": "Ambiguous"}}}
    attempts = runner._checkpoint_attempts_for_candidate(checkpoint, candidate)
    assert runner._excluded_models(attempts) == set()
    checkpoint.update(state="retry_requested", source_fingerprint=runner.personality_source_fingerprint(candidate))
    assert runner._excluded_models(runner._checkpoint_attempts_for_candidate(checkpoint, candidate)) == set()


def test_negative_decisions_reopen_for_a_relevant_contract_change():
    candidate = PersonalityCandidate("Raw", 1, 1, ("author",))
    checkpoint = {"raw_name": "Raw", "source_fingerprint": runner.personality_source_fingerprint(candidate),
        "state": "unusable", "schema_version": "previous-outcome-contract", "prompt_version": "previous-rules"}
    assert runner._eligible_candidates([candidate], [checkpoint])[0] == [candidate]


def test_parallel_tail_waits_for_the_pause_and_all_first_pass_workers(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone
    import threading

    pause_started = threading.Event()
    release_pause = threading.Event()
    other_done = threading.Event()
    retry_started = threading.Event()
    attempts = []
    db = Db()

    class PauseManager(Manager):
        def _sleep_until(self, when):
            pause_started.set()
            assert release_pause.wait(3)

    def pool(**kwargs):
        raw = kwargs["request"]("first", "fake-key", None)
        name = json.loads(raw)["name_full"]
        attempts.append(name)
        if name == "A" and attempts.count("A") == 1:
            raise GeminiModelPoolOperationalError("503", retryable=True, retry_at=datetime.now(timezone.utc))
        if name == "B":
            assert pause_started.wait(3)
            other_done.set()
        if name == "A":
            retry_started.set()
        return SimpleNamespace(model_name="first", value=kwargs["parse"](raw))

    monkeypatch.setattr(runner, "GeminiRuntimeManager", PauseManager)
    monkeypatch.setattr(runner, "run_ordered_model_pool", pool)

    def request(**kwargs):
        name = kwargs["contents"][0].split("<raw_name>")[1].split("</raw_name>")[0]
        return response(name_full=name)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner.run_personality_normalization, db=db, models=["first"], run_id=1,
            should_stop=lambda: False, workers=2, request_json=request,
            candidates=[PersonalityCandidate(name, 1, 1, ("author",)) for name in ("A", "B")])
        try:
            assert other_done.wait(3)
            assert not retry_started.wait(0.2)
        finally:
            release_pause.set()
        assert future.result(timeout=3)["succeeded"] == 2
    assert attempts == ["A", "B", "A"]


def test_model_transient_history_is_retained_and_never_excluded(monkeypatch):
    monkeypatch.setattr(runner, "GeminiRuntimeManager", Manager)
    db = Db()
    seen = []

    def pool(**kwargs):
        seen.append(kwargs["already_attempted"])
        raise GeminiModelPoolOperationalError("429 cooldown", retryable=True, model_name="first")

    monkeypatch.setattr(runner, "run_ordered_model_pool", pool)
    runner.run_personality_normalization(db=db, models=["first"], run_id=None, should_stop=lambda: False,
        candidates=[PersonalityCandidate("Raw", 1, 1, ("author",))])
    assert seen == [set(), set()]
    failure = db.checkpoints["Raw"]["attempted_models"]["first"]
    assert failure["kind"] == "transient"
    assert failure["previous_failure"]["error"] == "429 cooldown"


def test_reason_length_is_bounded():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        PersonalityResponse.model_validate_json(response("unusable", "x" * 301))
