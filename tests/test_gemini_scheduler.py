from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest

from app.local_state import LocalStateStore
from app.repositories.gemini import GeminiRepository


class Repository(GeminiRepository):
    def __init__(self, path):
        self.store = LocalStateStore(path)
        self.store.initialize()
        self._lock = threading.RLock()

    @contextmanager
    def _runtime_connect(self, **kwargs):
        with self.store.connect(**kwargs) as conn:
            yield conn


def setup_repo(tmp_path):
    repo = Repository(tmp_path / "runtime.sqlite3")
    repo.upsert_gemini_keys(
        [
            dict(
                key_id=f"k{i}",
                account_id=f"a{i // 2}",
                quota_domain_id=f"p{i}",
                masked_key="masked",
            )
            for i in range(4)
        ]
    )
    for model in ["m1", "m2"]:
        repo.ensure_gemini_model_states(["k0", "k1", "k2", "k3"], model)
    return repo


def claim(repo, models=("m1", "m2"), now="2026-09-27T12:00:00+00:00"):
    from datetime import datetime, timedelta

    timestamp = datetime.fromisoformat(now)
    return repo.claim_gemini_ready_request(
        models=models,
        key_ids=["k0", "k1", "k2", "k3"],
        now_ts=now,
        cooldown_until=(timestamp + timedelta(seconds=60)).isoformat(),
        expires_at=(timestamp + timedelta(seconds=90)).isoformat(),
        lease_token=threading.current_thread().name,
        task_id="test",
        run_id=None,
        worker_id="test",
    )


def test_atomic_round_robin_diversifies_accounts_and_uses_busy_account_projects(
    tmp_path,
):
    repo = setup_repo(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: claim(repo), range(4)))
    assert [r["model_name"] for r in results].count("m1") == 2
    assert [r["model_name"] for r in results].count("m2") == 2
    assert len({r["quota_domain_id"] for r in results}) == 4
    assert claim(repo)["retry_at"] == "2026-09-27T12:01:30+00:00"


def test_paused_models_skipped_and_project_spacing_survives_release(tmp_path):
    repo = setup_repo(tmp_path)
    repo.set_gemini_model_pause("m1", "2026-09-27T12:01:00+00:00", "503")
    first = claim(repo)
    assert first["model_name"] == "m2"
    repo.release_gemini_project_lease(first["quota_domain_id"], first["lease_token"])
    second = claim(repo, models=("m2",))
    assert second["quota_domain_id"] != first["quota_domain_id"]


def test_project_keys_share_lease_and_spacing(tmp_path):
    repo = setup_repo(tmp_path)
    with repo._runtime_connect() as conn:
        conn.execute("UPDATE gemini_keys SET quota_domain_id='shared'")
    first = claim(repo, models=("m1",))
    assert "key_id" in first
    assert "key_id" not in claim(repo, models=("m2",))
    repo.release_gemini_project_lease("shared", first["lease_token"])
    assert claim(repo, models=("m1",))["retry_at"] == "2026-09-27T12:01:00+00:00"
    assert claim(repo, models=("m2",))["model_name"] == "m2"


def test_runtime_skips_503_model_on_next_request_and_releases_project(
    tmp_path, monkeypatch
):
    from datetime import datetime, timezone
    from app.gemini_config import GeminiKey
    from app.gemini_runtime import GeminiRuntimeManager, GeminiServerPauseError
    import pytest

    repo = setup_repo(tmp_path)
    repo.insert_event = lambda *args, **kwargs: None
    manager = GeminiRuntimeManager(repo, task_id="test", panel_id=None)
    keys = [
        GeminiKey(f"a{i // 2}", f"k{i}", "secret", "masked", f"p{i}") for i in range(4)
    ]
    monkeypatch.setattr(manager, "_sync_key_registry", lambda: keys)
    monkeypatch.setattr(manager, "_ensure_cycle", lambda now: {})
    monkeypatch.setattr(manager, "_wait_reason", lambda *args: None)
    monkeypatch.setattr(
        "app.gemini_runtime._utc_now",
        lambda: datetime(2026, 9, 27, 12, tzinfo=timezone.utc),
    )

    class ServerError(Exception):
        code = 503

    def fail(model, key, lease):
        raise ServerError("busy")

    with pytest.raises(GeminiServerPauseError):
        manager.run_with_available_model(models=["m1", "m2"], call=fail)
    model, value = manager.run_with_available_model(
        models=["m1", "m2"], call=lambda model, key, lease: "ok"
    )
    assert (model, value) == ("m2", "ok")
    with repo._runtime_connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM gemini_project_leases WHERE lease_token IS NOT NULL"
            ).fetchone()["n"]
            == 0
        )


def test_excluded_item_does_not_wait_while_other_model_can_serve(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from app.gemini_config import GeminiKey
    from app.gemini_runtime import GeminiRuntimeManager, GeminiQuotaCooldownError
    import pytest

    repo = setup_repo(tmp_path)
    repo.insert_event = lambda *args, **kwargs: None
    repo.set_gemini_model_pause("m1", "2026-09-27T12:01:00+00:00", "503")
    manager = GeminiRuntimeManager(repo, task_id="test", panel_id=None)
    monkeypatch.setattr(
        manager,
        "_sync_key_registry",
        lambda: [GeminiKey("a0", "k0", "secret", "masked", "p0")],
    )
    monkeypatch.setattr(manager, "_ensure_cycle", lambda now: {})
    monkeypatch.setattr(manager, "_wait_reason", lambda *args: None)
    monkeypatch.setattr(
        "app.gemini_runtime._utc_now",
        lambda: datetime(2026, 9, 27, 12, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        manager, "_sleep_until", lambda when: pytest.fail("must yield item, not sleep")
    )
    with pytest.raises(GeminiQuotaCooldownError):
        manager.run_with_available_model(
            models=["m1"], pool_models=["m1", "m2"], call=lambda *args: "unused"
        )


def _process_claim(path):
    return claim(Repository(path))


def test_cross_process_cursor_and_project_claims(tmp_path):
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing

    repo = setup_repo(tmp_path)
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("fork")
    ) as pool:
        results = list(pool.map(_process_claim, [repo.store.path] * 4))
    assert len({r["quota_domain_id"] for r in results}) == 4
    assert sorted(r["model_name"] for r in results) == ["m1", "m1", "m2", "m2"]


def test_lease_ownership_and_expiry_recovery(tmp_path):
    repo = setup_repo(tmp_path)
    first = claim(repo)
    domain = first["quota_domain_id"]
    assert not repo.release_gemini_project_lease(domain, "wrong-owner")
    assert not repo.renew_gemini_project_lease(
        domain, "wrong-owner", expires_at="later", now_ts="now"
    )
    assert repo.renew_gemini_project_lease(
        domain,
        first["lease_token"],
        expires_at="2026-09-27T12:02:00+00:00",
        now_ts="2026-09-27T12:00:30+00:00",
    )
    claim(repo)
    claim(repo)
    claim(repo)
    assert "key_id" not in claim(repo)
    recovered = claim(repo, now="2026-09-27T12:03:00+00:00")
    assert "key_id" in recovered


def test_ready_capacity_is_not_capped_at_ten_requests_per_minute(tmp_path):
    repo = setup_repo(tmp_path)
    repo.upsert_gemini_keys(
        [
            dict(
                key_id=f"k{i}",
                account_id=f"a{i}",
                quota_domain_id=f"p{i}",
                masked_key="masked",
            )
            for i in range(12)
        ]
    )
    ids = [f"k{i}" for i in range(12)]
    repo.ensure_gemini_model_states(ids, "m1")
    for index in range(12):
        result = repo.claim_gemini_ready_request(
            models=["m1"],
            key_ids=ids,
            now_ts="2026-09-27T12:00:00+00:00",
            cooldown_until="2026-09-27T12:01:00+00:00",
            expires_at="2026-09-27T12:01:30+00:00",
            lease_token=str(index),
            task_id=f"task{index}",
            run_id=None,
            worker_id=str(index),
        )
        assert "key_id" in result


def test_idle_account_precedes_unused_project_in_busy_account(tmp_path):
    repo = setup_repo(tmp_path)
    first = claim(repo)
    second = claim(repo)
    assert first["account_id"] != second["account_id"]
    third = claim(repo)
    assert third["quota_domain_id"] not in {
        first["quota_domain_id"],
        second["quota_domain_id"],
    }


def test_quota_cooldown_and_daily_exhaustion_are_scoped_to_project_model(tmp_path):
    repo = setup_repo(tmp_path)
    repo.set_gemini_quota_domain_model_cooldown(
        "p0",
        "m1",
        cooldown_until="2026-09-27T12:02:00+00:00",
        failure_count=1,
        now_ts="2026-09-27T12:00:00+00:00",
        error_text="429 RPM",
    )
    first = claim(repo, models=("m1",))
    assert first["quota_domain_id"] != "p0"
    for domain in ["p0", "p1", "p2", "p3"]:
        repo.mark_gemini_quota_domain_model_exhausted(
            domain, "m1", now_ts="2026-09-27T12:00:00+00:00", error_text="daily quota"
        )
    assert claim(repo, models=("m1",)) == {"retry_at": None}
    assert claim(repo)["model_name"] == "m2"


def test_v3_upgrade_preserves_quota_evidence_and_project_spacing(tmp_path):
    import sqlite3

    repo = setup_repo(tmp_path)
    repo.mark_gemini_quota_domain_model_exhausted(
        "p0", "m1", now_ts="2026-09-27T12:00:00+00:00", error_text="daily quota"
    )
    with sqlite3.connect(repo.store.path) as conn:
        conn.execute("PRAGMA user_version=3")
        conn.execute("DROP TABLE gemini_project_model_spacing")
        conn.execute(
            "UPDATE gemini_key_model_state SET cooldown_until='2026-09-27T12:01:00+00:00' WHERE key_id='k1' AND model_name='m2'"
        )
    repo.store.initialize()
    with repo._runtime_connect() as conn:
        assert (
            conn.execute(
                "SELECT next_request_at FROM gemini_project_model_spacing WHERE quota_domain_id='p1' AND model_name='m2'"
            ).fetchone()["next_request_at"]
            == "2026-09-27T12:01:00+00:00"
        )
        assert (
            conn.execute(
                "SELECT exhausted FROM gemini_key_model_state WHERE key_id='k0' AND model_name='m1'"
            ).fetchone()["exhausted"]
            == 1
        )


def test_personality_503_moves_to_tail_and_next_person_uses_another_model(
    tmp_path, monkeypatch
):
    import json
    from datetime import datetime, timezone
    from app.gemini_config import GeminiKey
    from app.gemini_runtime import GeminiRuntimeManager
    from app.modules.library.personality_normalization import (
        PersonalityCandidate,
        PersonComponents,
    )
    from app.modules.library.runtime import run_normalize_personalities as runner

    repo = setup_repo(tmp_path)
    checkpoints = {}
    repo.list_personality_checkpoints = lambda: list(checkpoints.values())
    repo.get_personality_checkpoint = checkpoints.get

    def save(**values):
        checkpoints[values["raw_name"]] = values

    repo.save_personality_checkpoint = save

    def persist(**values):
        save(**values, state="succeeded")
        return {"canonical_id": 1}

    repo.persist_personality_normalization = persist
    repo.publish_run_progress = lambda **kwargs: None
    repo.insert_event = lambda *args, **kwargs: None
    manager = GeminiRuntimeManager(repo, task_id="test", panel_id=None)
    monkeypatch.setattr(
        manager,
        "_sync_key_registry",
        lambda: [
            GeminiKey(f"a{i // 2}", f"k{i}", "secret", "masked", f"p{i}")
            for i in range(4)
        ],
    )
    monkeypatch.setattr(manager, "_ensure_cycle", lambda now: {})
    monkeypatch.setattr(manager, "_wait_reason", lambda *args: None)
    monkeypatch.setattr(
        manager,
        "_sleep_until",
        lambda when: pytest.fail("ready models must keep working"),
    )
    monkeypatch.setattr(
        "app.gemini_runtime._utc_now",
        lambda: datetime(2026, 9, 27, 12, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(runner, "GeminiRuntimeManager", lambda *args, **kwargs: manager)
    calls = []

    class ServerError(Exception):
        code = 503

    def request(**kwargs):
        name = kwargs["contents"][0].split("<raw_name>")[1].split("</raw_name>")[0]
        calls.append((name, kwargs["model_name"]))
        if len(calls) == 1:
            raise ServerError("busy")
        return json.dumps(
            {
                **dict.fromkeys(PersonComponents.model_fields),
                "outcome": "normalized",
                "reason": None,
                "name_full": name,
            }
        )

    summary = runner.run_personality_normalization(
        db=repo,
        models=["m1", "m2"],
        run_id=None,
        should_stop=lambda: False,
        workers=1,
        request_json=request,
        candidates=[
            PersonalityCandidate(name, 1, 1, ("author",)) for name in ["A", "B"]
        ],
    )
    assert calls == [("A", "m1"), ("B", "m2"), ("A", "m2")]
    assert summary["succeeded"] == summary["processed"] == 2
    assert summary["model_attempts"] == {"m1": 1, "m2": 2}


def test_scheduler_rechecks_capacity_released_before_lease_expiry(
    tmp_path, monkeypatch
):
    from datetime import datetime, timezone, timedelta
    from app.gemini_config import GeminiKey
    from app.gemini_runtime import GeminiRuntimeManager

    repo = setup_repo(tmp_path)
    held = [claim(repo) for _ in range(4)]
    repo.insert_event = lambda *args, **kwargs: None
    now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    manager = GeminiRuntimeManager(repo, task_id="test", panel_id=None)
    monkeypatch.setattr(
        manager,
        "_sync_key_registry",
        lambda: [
            GeminiKey(f"a{i // 2}", f"k{i}", "secret", "masked", f"p{i}")
            for i in range(4)
        ],
    )
    monkeypatch.setattr(manager, "_ensure_cycle", lambda time: {})
    monkeypatch.setattr(manager, "_wait_reason", lambda *args: None)
    monkeypatch.setattr("app.gemini_runtime._utc_now", lambda: now)
    waits = []

    def release_early(until):
        nonlocal now
        waits.append(until)
        assert until <= now + timedelta(seconds=1)
        now = until
        repo.release_gemini_project_lease(
            held[0]["quota_domain_id"], held[0]["lease_token"]
        )

    monkeypatch.setattr(manager, "_sleep_until", release_early)
    model, response = manager.run_with_available_model(
        models=["m1", "m2"], call=lambda *args: "ok"
    )
    assert response == "ok"
    assert len(waits) == 1


def test_bounded_retry_rotates_model_and_returns_successful_model(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from app.gemini_config import GeminiKey
    from app.gemini_runtime import GeminiRuntimeManager
    repo = setup_repo(tmp_path)
    repo.insert_event = lambda *args, **kwargs: None
    manager = GeminiRuntimeManager(repo, task_id='test', panel_id=None)
    monkeypatch.setattr(manager, '_sync_key_registry', lambda: [
        GeminiKey(f'a{i // 2}', f'k{i}', 'secret', 'masked', f'p{i}') for i in range(4)
    ])
    monkeypatch.setattr(manager, '_ensure_cycle', lambda now: {})
    monkeypatch.setattr(manager, '_wait_reason', lambda *args: None)
    monkeypatch.setattr(manager, '_sleep_until', lambda when: pytest.fail('another model is ready'))
    monkeypatch.setattr('app.gemini_runtime._utc_now', lambda: datetime(2026, 9, 27, 12, tzinfo=timezone.utc))
    calls = []
    class ServerError(Exception):
        code = 503
    def request(model, key, lease):
        calls.append(model)
        if len(calls) == 1:
            raise ServerError('busy')
        return 'ok'
    assert manager.run_with_available_model(models=['m1', 'm2'], call=request, max_attempts=2) == ('m2', 'ok')
    assert calls == ['m1', 'm2']


def test_429_rotates_to_another_ready_model_account_and_project(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from app.gemini_config import GeminiKey
    from app.gemini_runtime import GeminiRuntimeManager
    from app.gemini_model_pool import run_ordered_model_pool
    repo = setup_repo(tmp_path)
    repo.insert_event = lambda *args, **kwargs: None
    manager = GeminiRuntimeManager(repo, task_id='test', panel_id=None)
    monkeypatch.setattr(manager, '_sync_key_registry', lambda: [
        GeminiKey(f'a{i // 2}', f'k{i}', 'secret', 'masked', f'p{i}') for i in range(4)
    ])
    monkeypatch.setattr(manager, '_ensure_cycle', lambda now: {})
    monkeypatch.setattr(manager, '_wait_reason', lambda *args: None)
    monkeypatch.setattr(manager, '_sleep_until', lambda when: pytest.fail('alternate capacity is ready'))
    monkeypatch.setattr('app.gemini_runtime._utc_now', lambda: datetime(2026, 9, 27, 12, tzinfo=timezone.utc))
    calls = []
    class QuotaError(Exception):
        code = 429
    def request(model, key, lease):
        calls.append((model, lease.account_id, lease.quota_domain_id))
        if len(calls) == 1:
            raise QuotaError('Requests per minute quota')
        return 'ok'
    result = run_ordered_model_pool(manager=manager, models=['m1', 'm2'], request=request,
        parse=lambda raw: raw, record_failure=lambda *args: pytest.fail('quota is not a content failure'), run_id=None)
    assert result.value == 'ok'
    assert [call[0] for call in calls] == ['m1', 'm2']
    assert calls[0][1:] != calls[1][1:]
    assert repo.get_gemini_quota_domain_model_state('p0', 'm1')['failure_count'] == 1


def test_generation_spacing_starts_after_upload_preparation(tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from app.gemini_config import GeminiKey
    from app.gemini_runtime import GeminiRuntimeManager, record_gemini_generation_start
    repo = setup_repo(tmp_path)
    repo.insert_event = lambda *args, **kwargs: None
    manager = GeminiRuntimeManager(repo, task_id='test', panel_id=None)
    now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(manager, '_sync_key_registry', lambda: [GeminiKey('a0', 'k0', 'secret', 'masked', 'p0')])
    monkeypatch.setattr(manager, '_ensure_cycle', lambda time: {})
    monkeypatch.setattr(manager, '_wait_reason', lambda *args: None)
    monkeypatch.setattr('app.gemini_runtime._utc_now', lambda: now)
    def request(model, key, lease):
        nonlocal now
        now += timedelta(seconds=80)
        record_gemini_generation_start()
        now += timedelta(seconds=5)
        return 'ok'
    assert manager.run_with_available_model(models=['m1'], call=request) == ('m1', 'ok')
    with repo._runtime_connect() as conn:
        assert conn.execute("SELECT next_request_at FROM gemini_project_model_spacing WHERE quota_domain_id='p0' AND model_name='m1'").fetchone()['next_request_at'] == '2026-09-27T12:02:20+00:00'
    # The scoped callback must not leak into an unrelated call after release.
    record_gemini_generation_start()
