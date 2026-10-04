"""Run-scoped pacing against real SQLite with deterministic timestamps."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.gemini_pacing import GeminiPacingPolicy
from test_gemini_scheduler import setup_repo

BASE = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def ts(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat()


def claim(repo, seconds=0, *, scope="run-1", token="owner", models=("m1", "m2"), paced=True):
    return repo.claim_gemini_ready_request(
        models=models, key_ids=["k0", "k1", "k2", "k3"], now_ts=ts(seconds),
        cooldown_until=ts(seconds + 60), expires_at=ts(seconds + 90),
        lease_token=token, task_id="personalities", run_id=None, worker_id=token,
        **({"pacing_policy": GeminiPacingPolicy(scope)} if paced else {}),
    )


def start(repo, item, seconds, scope="run-1"):
    return repo.record_gemini_generation_start(
        item["quota_domain_id"], item["model_name"], key_id=item["key_id"],
        lease_token=item["lease_token"], now_ts=ts(seconds),
        next_request_at=ts(seconds + 60), expires_at=ts(seconds + 90),
        pacing_policy=GeminiPacingPolicy(scope), pacing_epoch=item["pacing_epoch"],
    )


def finish(repo, item, outcome, seconds, scope="run-1"):
    result = repo.record_gemini_pacing_outcome(
        GeminiPacingPolicy(scope), quota_domain_id=item["quota_domain_id"],
        lease_token=item["lease_token"], epoch=item["pacing_epoch"],
        probe=item["pacing_probe"], outcome=outcome, now_ts=ts(seconds),
    )
    repo.release_gemini_project_lease(item["quota_domain_id"], item["lease_token"])
    return result


def attempt(repo, seconds, outcome, *, scope="run-1", token="owner"):
    item = claim(repo, seconds, scope=scope, token=token)
    assert "key_id" in item, item
    assert start(repo, item, seconds, scope) is True
    finish(repo, item, outcome, seconds + .6, scope)
    return item


def trip(repo):
    for i, seconds in enumerate((0, 10, 30)):
        attempt(repo, seconds, "quota", token=f"failure-{i}")


def test_fast_successes_are_spaced_across_workers_and_models(tmp_path):
    repo = setup_repo(tmp_path)
    first = claim(repo)
    assert start(repo, first, 0) is True
    finish(repo, first, "success", 3)
    blocked = claim(repo, 3, token="peer")
    assert blocked == {"retry_at": ts(5), "wait_reason": "pacing"}
    peer = claim(repo, 5, token="peer")
    assert peer["model_name"] != first["model_name"]
    assert start(repo, peer, 5) is True


def test_slow_response_needs_no_additional_delay(tmp_path):
    repo = setup_repo(tmp_path)
    first = claim(repo)
    start(repo, first, 0)
    finish(repo, first, "success", 8)
    assert "key_id" in claim(repo, 8, token="next")


def test_preparation_reserves_admission_and_spacing_uses_actual_start(tmp_path):
    repo = setup_repo(tmp_path)
    first = claim(repo)
    assert "key_id" not in claim(repo, 5, token="peer")
    assert start(repo, first, 8) is True
    assert claim(repo, 12, token="peer")["retry_at"] == ts(13)
    assert "key_id" in claim(repo, 13, token="peer")


def test_three_quota_failures_pause_and_only_one_worker_can_probe(tmp_path):
    repo = setup_repo(tmp_path)
    trip(repo)
    assert claim(repo, 90)["retry_at"] == ts(90.6)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda i: claim(repo, 91, token=f"probe-{i}"), range(4)))
    probes = [r for r in results if "key_id" in r]
    assert len(probes) == 1
    assert probes[0]["pacing_probe"] is True
    assert start(repo, probes[0], 91) is True
    assert "key_id" not in claim(repo, 140, token="peer")


def test_failed_probes_escalate_cooldown_and_success_recovers_gradually(tmp_path):
    repo = setup_repo(tmp_path)
    trip(repo)
    for seconds, delay in ((91, 120), (212, 240), (453, 480), (934, 600), (1535, 600)):
        probe = claim(repo, seconds, token=f"probe-{seconds}")
        assert probe["pacing_probe"] is True
        start(repo, probe, seconds)
        result = finish(repo, probe, "quota", seconds + .6)
        assert result["state"]["cooldown_until"] == ts(seconds + .6 + delay)
    probe = claim(repo, 2136, token="good-probe")
    start(repo, probe, 2136)
    result = finish(repo, probe, "success", 2137)
    assert result["state"]["mode"] == "recovery"
    assert result["interval_seconds"] == 60
    current = 2196
    for expected in (40, 20, 10, 5):
        for _ in range(5):
            item = claim(repo, current, token=f"healthy-{current}")
            start(repo, item, current)
            result = finish(repo, item, "success", current + 1)
            current += result["interval_seconds"]
        assert result["interval_seconds"] == expected
    assert result["state"]["mode"] == "normal"
    assert result["state"]["cooldown_level"] == 0


def test_quota_during_recovery_pauses_immediately(tmp_path):
    repo = setup_repo(tmp_path)
    trip(repo)
    attempt(repo, 91, "success", token="probe")
    item = claim(repo, 131, token="recovery")
    start(repo, item, 131)
    result = finish(repo, item, "quota", 132)
    assert result["state"]["cooldown_until"] == ts(252)


@pytest.mark.parametrize("outcome", ["transient", "neutral"])
def test_normal_non_quota_errors_do_not_trigger_adaptive_cooldown(tmp_path, outcome):
    repo = setup_repo(tmp_path)
    for seconds in (0, 5, 10):
        attempt(repo, seconds, outcome, token=str(seconds))
    assert "key_id" in claim(repo, 15)


def test_transient_probe_failure_extends_pause(tmp_path):
    repo = setup_repo(tmp_path)
    trip(repo)
    item = claim(repo, 91)
    start(repo, item, 91)
    result = finish(repo, item, "transient", 92)
    assert result["state"]["cooldown_until"] == ts(212)


def test_stale_inflight_success_cannot_reopen_cooldown(tmp_path):
    repo = setup_repo(tmp_path)
    stale = claim(repo, token="stale")
    start(repo, stale, 0)
    for seconds in (5, 15, 35):
        attempt(repo, seconds, "quota", token=str(seconds))
    assert finish(repo, stale, "success", 36) is None
    assert claim(repo, 37)["retry_at"] == ts(95.6)


def test_new_run_resets_adaptive_state_and_other_tasks_are_unpaced(tmp_path):
    repo = setup_repo(tmp_path)
    trip(repo)
    assert "key_id" in claim(repo, 35, scope="run-2", token="new-run")
    assert "key_id" in claim(repo, 35, paced=False, token="other-task")
    assert "key_id" not in claim(repo, 35, token="old-run")


def test_released_or_expired_probe_can_be_reclaimed_and_old_owner_ignored(tmp_path):
    repo = setup_repo(tmp_path)
    trip(repo)
    abandoned = claim(repo, 91, token="abandoned")
    start(repo, abandoned, 91)
    reclaimed = claim(repo, 182, token="reclaimed")
    assert reclaimed["pacing_probe"] is True
    assert finish(repo, abandoned, "success", 183) is None
    repo.release_gemini_project_lease(reclaimed["quota_domain_id"], reclaimed["lease_token"])
    assert claim(repo, 183, token="released-peer")["pacing_probe"] is True


def test_project_heartbeat_renews_probe_reservation(tmp_path):
    repo = setup_repo(tmp_path)
    trip(repo)
    probe = claim(repo, 91, token="long-probe")
    start(repo, probe, 91)
    assert repo.renew_gemini_project_lease(probe["quota_domain_id"], "long-probe", now_ts=ts(150), expires_at=ts(240))
    assert claim(repo, 182, token="peer")["retry_at"] == ts(240)


def test_existing_project_cooldown_and_exhaustion_take_precedence(tmp_path):
    repo = setup_repo(tmp_path)
    trip(repo)
    for domain in ("p0", "p1", "p2", "p3"):
        for model in ("m1", "m2"):
            repo.set_gemini_quota_domain_model_cooldown(domain, model, cooldown_until=ts(500), failure_count=1, now_ts=ts(35), error_text="quota")
    assert claim(repo, 91)["retry_at"] == ts(500)
    with repo._runtime_connect() as conn:
        conn.execute("UPDATE gemini_key_model_state SET exhausted=1")
    assert claim(repo, 91) == {"retry_at": None}


def runtime(repo, monkeypatch, clock, *, paced=True):
    from app.gemini_config import GeminiKey
    from app.gemini_runtime import GeminiRuntimeManager
    events = []
    repo.insert_event = lambda kind, **kwargs: events.append((kind, kwargs))
    manager = GeminiRuntimeManager(
        repo, task_id="personalities", panel_id=None,
        **({"pacing_policy": GeminiPacingPolicy("runtime-run")} if paced else {}),
    )
    monkeypatch.setattr(manager, "_sync_key_registry", lambda: [
        GeminiKey(f"a{i // 2}", f"k{i}", "secret", "masked", f"p{i}") for i in range(4)
    ])
    monkeypatch.setattr(manager, "_ensure_cycle", lambda now: {})
    monkeypatch.setattr(manager, "_wait_reason", lambda *args: None)
    monkeypatch.setattr("app.gemini_runtime._utc_now", lambda: BASE + timedelta(seconds=clock[0]))

    def sleep(until):
        if manager.should_stop():
            from app.gemini_runtime import GeminiStopRequestedError
            raise GeminiStopRequestedError("Stopped during pacing")
        clock[0] = (until - BASE).total_seconds()
    monkeypatch.setattr(manager, "_sleep_until", sleep)
    return manager, events


def test_runtime_spaces_generation_and_default_caller_remains_unpaced(tmp_path, monkeypatch):
    from app.gemini_runtime import record_gemini_generation_start
    repo = setup_repo(tmp_path)
    clock = [0]
    manager, events = runtime(repo, monkeypatch, clock)
    starts = []

    def request(model, key, lease):
        record_gemini_generation_start()
        starts.append(clock[0])
        clock[0] += 3
        return "ok"
    for _ in range(2):
        manager.run_with_available_model(models=["m1", "m2"], call=request)
    assert starts == [0, 5]
    assert sum(kind == "gemini.pacing.changed" for kind, _ in events) == 1
    unpaced, _ = runtime(repo, monkeypatch, clock, paced=False)
    unpaced.run_with_available_model(models=["m1", "m2"], call=request)
    assert starts[-1] == 8


def test_runtime_recovers_content_failure_as_provider_success(tmp_path, monkeypatch):
    from app.gemini_runtime import GeminiResponseValidationError, record_gemini_generation_start
    repo = setup_repo(tmp_path)
    clock = [0]
    manager, events = runtime(repo, monkeypatch, clock)

    class QuotaError(Exception):
        code = 429

    def quota(model, key, lease):
        record_gemini_generation_start()
        clock[0] += .6
        raise QuotaError("429 RESOURCE_EXHAUSTED")
    from app.gemini_runtime import GeminiQuotaExceededError
    for _ in range(3):
        with pytest.raises(GeminiQuotaExceededError):
            manager.run_with_available_model(models=["m1", "m2"], call=quota)

    def invalid(model, key, lease):
        record_gemini_generation_start()
        clock[0] += 1
        raise GeminiResponseValidationError("Invalid JSON")
    with pytest.raises(GeminiResponseValidationError):
        manager.run_with_available_model(models=["m1", "m2"], call=invalid)
    assert clock[0] >= 91.6
    assert any(kind == "gemini.pacing.changed" and values["payload"]["mode"] == "recovery" for kind, values in events)
    assert "key_id" in claim(repo, clock[0] + 40, scope="runtime-run")


def test_stop_during_pacing_does_not_claim_project_or_consume_attempt(tmp_path, monkeypatch):
    from app.gemini_runtime import GeminiStopRequestedError, record_gemini_generation_start
    repo = setup_repo(tmp_path)
    clock = [0]
    manager, _ = runtime(repo, monkeypatch, clock)
    manager.run_with_available_model(models=["m1", "m2"], call=lambda *args: record_gemini_generation_start())
    manager.should_stop = lambda: True
    with pytest.raises(GeminiStopRequestedError):
        manager.run_with_available_model(models=["m1", "m2"], call=lambda *args: pytest.fail("request after stop"))
    with repo._runtime_connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM gemini_project_leases WHERE lease_token IS NOT NULL").fetchone()["n"] == 0


def test_runtime_reacquires_stale_preparation_without_sending_or_spending_retry(tmp_path, monkeypatch):
    from app.gemini_runtime import record_gemini_generation_start
    repo = setup_repo(tmp_path)
    clock = [0]
    manager, _ = runtime(repo, monkeypatch, clock)
    calls = []

    def request(model, key, lease):
        if not calls:
            # Other in-flight requests tripped the queue while this request prepared.
            with repo._runtime_connect() as conn:
                import json
                row = conn.execute("SELECT state_json FROM gemini_task_pacing").fetchone()
                state = json.loads(row["state_json"])
                state.update(mode="cooldown", epoch=state["epoch"] + 1, cooldown_until=ts(60), level=3)
                conn.execute("UPDATE gemini_task_pacing SET state_json=?", (json.dumps(state),))
            calls.append("cancelled-preparation")
        record_gemini_generation_start()
        calls.append("sent")
        return "ok"
    assert manager.run_with_available_model(models=["m1", "m2"], call=request)[1] == "ok"
    assert calls == ["cancelled-preparation", "sent"]
    assert clock[0] == 60


def test_probe_generation_renews_reservation_after_preparation(tmp_path):
    repo = setup_repo(tmp_path)
    trip(repo)
    probe = claim(repo, 91, token="prepared-probe")
    assert start(repo, probe, 140) is True
    assert claim(repo, 182, token="peer")["retry_at"] == ts(230)
