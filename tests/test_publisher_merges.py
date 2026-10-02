from copy import deepcopy

import pytest

from app.modules.library.publisher_merge_contract import (
    ClusterResponse,
    build_prompt,
    inventory_fingerprint,
    resolve_scope,
    validate_response,
)
from app.modules.library.publisher_codex import CodexSettings, quota_observations


@pytest.fixture(autouse=True)
def isolated_test_codex_home(monkeypatch, tmp_path):
    home = tmp_path / "empty-codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))


@pytest.fixture
def inventory():
    return [
        {
            "key": "canonical:7",
            "display_name": "Татар китап",
            "aliases": ["Татар китап", "Tatar kitap"],
            "is_new": False,
            "document_count": 3,
            "canonical_id": 7,
            "raw_name": None,
        },
        {
            "key": "raw:TK",
            "display_name": "TK",
            "aliases": [],
            "is_new": True,
            "document_count": 2,
            "canonical_id": None,
            "raw_name": "TK",
        },
        {
            "key": "canonical:8",
            "display_name": "Other",
            "aliases": ["Other"],
            "is_new": False,
            "document_count": 1,
            "canonical_id": 8,
            "raw_name": None,
        },
    ]


def proposal(members=None):
    return {
        "clusters": [
            {
                "member_ids": members or ["canonical:7", "raw:TK"],
                "proposed_name": "Татар китап",
                "rationale": "Same identity supported by the inventory.",
                "kind": "cluster",
                "confidence": "uncertain",
                "uncertainty": "Acronym needs review.",
                "citations": [
                    {
                        "url": "https://example.org/history",
                        "supports": "Official history.",
                    }
                ],
            }
        ]
    }


def test_cross_script_uncertain_response_and_prompt(inventory):
    groups = validate_response(proposal(), inventory, "new", [])
    assert groups[0]["confidence"] == "uncertain"
    prompt = build_prompt(inventory, "new", [])
    assert "Tatar kitap" in prompt and "TK" in prompt and "separation" in prompt
    assert ClusterResponse.model_json_schema()["additionalProperties"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["clusters"][0].update(member_ids=["raw:missing", "canonical:7"]),
        lambda p: p["clusters"][0].update(member_ids=["raw:TK", "raw:TK"]),
        lambda p: p["clusters"][0].update(member_ids=["raw:TK"]),
        lambda p: p["clusters"][0].update(proposed_name="Invented"),
        lambda p: p["clusters"][0].update(confidence=0.8),
        lambda p: p["clusters"][0].update(
            citations=[{"url": "javascript:alert(1)", "supports": "x"}]
        ),
        lambda p: p["clusters"][0].update(uncertainty=False),
    ],
)
def test_invalid_output_rejected(inventory, mutation):
    payload = proposal()
    mutation(payload)
    with pytest.raises(ValueError):
        validate_response(payload, inventory, "new", [])


def test_scope_and_separation_protect_approved_identities(inventory):
    assert resolve_scope("auto", False) == "all"
    assert resolve_scope("auto", True) == "new"
    assert resolve_scope("all", True) == "all"
    with pytest.raises(ValueError, match="established"):
        validate_response(
            proposal(["canonical:7", "canonical:8", "raw:TK"]), inventory, "new", []
        )
    with pytest.raises(ValueError, match="separation"):
        validate_response(proposal(), inventory, "new", [["canonical:7", "raw:TK"]])
    payload = proposal()
    payload["clusters"][0]["proposed_name"] = "TK"
    with pytest.raises(ValueError, match="chosen name"):
        validate_response(payload, inventory, "new", [])
    assert inventory_fingerprint(inventory) == inventory_fingerprint(
        deepcopy(inventory)
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("web_search", "maybe"),
        ("timeout_seconds", True),
        ("timeout_seconds", 1.5),
        ("timeout_seconds", 0),
        ("scope", "bad"),
        ("model", ""),
        ("reasoning_effort", "bad"),
    ],
)
def test_strict_config(field, value):
    with pytest.raises(ValueError):
        CodexSettings.from_config({"codex": {"publisher_merges": {field: value}}})


def test_config_preserves_requested_model_and_reasoning():
    settings = CodexSettings.from_config(
        {
            "codex": {
                "publisher_merges": {
                    "model": "custom",
                    "reasoning_effort": "high",
                    "web_search": "false",
                    "timeout_seconds": "60",
                }
            }
        }
    )
    assert settings.model == "custom" and settings.reasoning_effort == "high"
    assert settings.web_search is False and settings.timeout_seconds == 60


def quota(percent, reset=100, duration=300):
    return {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {
                    "usedPercent": percent,
                    "windowDurationMins": duration,
                    "resetsAt": reset,
                }
            }
        }
    }


def test_quota_comparability_resets_and_missing_windows():
    report = quota_observations(quota(12), quota(15))
    assert report["windows"][0]["delta_percentage_points"] == 3
    assert report["label"] == "account usage observed during this run"
    assert (
        quota_observations(quota(12), quota(1, reset=200))["windows"][0][
            "delta_percentage_points"
        ]
        is None
    )
    assert (
        quota_observations(None, quota(15))["windows"][0]["delta_percentage_points"]
        is None
    )
    assert quota_observations(None, None)["available"] is False


def test_inventory_distinct_documents_and_all_aliases(inventory):
    from app.modules.library.publisher_merge_contract import build_inventory

    class DB:
        def list_publisher_source_documents(self):
            return [
                {
                    "md5": "a",
                    "schema_org": {"publisher": ["Татар китап", "Tatar kitap", "TK"]},
                },
                {"md5": "b", "schema_org": {"publisher": "Tatar kitap"}},
            ]

        def list_normalization_canonicals(self, _):
            return [
                {"canonical_id": 7, "display_name": "Татар китап", "status": "active"}
            ]

        def list_normalization_aliases(self, _):
            return [
                {"canonical_id": 7, "raw_name": name, "decision_status": "linked"}
                for name in ["Татар китап", "Tatar kitap", "Unused alias"]
            ]

    rows = build_inventory(DB())
    assert rows[0]["document_count"] == 2
    assert rows[0]["aliases"] == ["Tatar kitap", "Unused alias", "Татар китап"]
    assert [row["key"] for row in rows] == ["canonical:7", "raw:TK"]


def test_durable_review_draft_apply_and_stale_conflicts(test_client):
    from app.modules.library.publisher_merge_review import review_action, save_draft
    from app.modules.library.publisher_workbench import apply_publishers

    _, app = test_client
    db = app.state.db
    db.apply_publisher_change_set({"renames": [], "keeps": ["Press"], "merges": []})
    canonical = db.list_normalization_canonicals("publisher")[0]
    cid = canonical["canonical_id"]
    rows = [
        {
            "key": f"canonical:{cid}",
            "canonical_id": cid,
            "raw_name": None,
            "display_name": "Press",
            "aliases": ["Press"],
            "is_new": False,
            "document_count": 1,
        },
        {
            "key": "raw:P.",
            "canonical_id": None,
            "raw_name": "P.",
            "display_name": "P.",
            "aliases": [],
            "is_new": True,
            "document_count": 1,
        },
    ]
    analysis = db.create_publisher_analysis(
        rows, inventory_fingerprint(rows), "new", {}
    )
    groups = proposal([f"canonical:{cid}", "raw:P."])["clusters"]
    groups[0]["proposed_name"] = "Press"
    db.checkpoint_publisher_analysis(analysis, groups, {})
    db.import_publisher_analysis(analysis)
    db.import_publisher_analysis(analysis)
    stored = db.get_publisher_review()
    assert len(stored["proposals"]) == 1
    pid = stored["proposals"][0]["proposal_id"]
    review_action(
        db,
        pid,
        {
            "action": "stage",
            "member_ids": [f"canonical:{cid}", "raw:P."],
            "display_name": "Press",
        },
    )
    assert len(db.get_publisher_review()["draft"]["merges"]) == 1
    apply_publishers(
        db, {"use_draft": True, "revision": db.get_publisher_review()["revision"]}
    )
    assert db.list_normalization_canonicals("publisher")[0]["canonical_id"] == cid
    assert db.list_normalization_canonicals("publisher")[0]["display_name"] == "Press"
    assert {a["raw_name"] for a in db.list_normalization_aliases("publisher")} == {
        "Press",
        "P.",
    }
    current = db.get_publisher_review()
    save_draft(
        db,
        {
            "revision": current["revision"],
            "changes": {"renames": [{"canonical_id": cid, "display_name": "Edited"}]},
        },
    )
    db.rename_normalization_canonical("publisher", cid, "Concurrent", "concurrent")
    with pytest.raises(ValueError, match="refresh"):
        apply_publishers(
            db, {"use_draft": True, "revision": db.get_publisher_review()["revision"]}
        )
    assert db.get_publisher_review()["draft"]["renames"][0]["display_name"] == "Edited"


def test_runtime_checkpoint_recovery_and_empty_new_scope(
    inventory, monkeypatch, tmp_path
):
    from app.modules.library.runtime import run_suggest_publisher_merges as runner

    class DB:
        state = {"successful": True, "checkpoint": None, "separations": []}

        def publisher_analysis_state(self):
            return self.state

        def import_publisher_analysis(self, aid):
            return 2

    class Adapter:
        def __init__(self, *args):
            raise AssertionError("Inference must not start")

    monkeypatch.setattr(runner, "build_inventory", lambda db: [inventory[0]])
    settings = CodexSettings(scope="auto")
    db = DB()
    result = runner.run_analysis(
        db=db,
        settings=settings,
        workspace=tmp_path,
        should_stop=lambda: False,
        adapter_factory=Adapter,
    )
    assert result["no_analysis_needed"] is True and result["scope"] == "new"
    db.state = {
        **db.state,
        "checkpoint": {
            "analysis_id": 1,
            "scope": "all",
            "inventory": inventory[:2],
            "response": proposal()["clusters"],
            "metadata": {"model": "custom"},
        },
    }
    result = runner.run_analysis(
        db=db,
        settings=settings,
        workspace=tmp_path,
        should_stop=lambda: False,
        adapter_factory=Adapter,
    )
    assert result["recovered"] is True and result["new_proposals"] == 2


def test_cli_environment_excludes_app_secrets_and_context_overflow(
    monkeypatch, tmp_path
):
    import json
    from types import SimpleNamespace
    from app.modules.library.publisher_codex import CodexAdapter

    monkeypatch.setenv("MANZARA_DATABASE_URL", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source"))
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
    adapter = CodexAdapter(CodexSettings(context_window_tokens=100), tmp_path)
    assert (
        "MANZARA_DATABASE_URL" not in adapter.env
        and "OPENAI_API_KEY" not in adapter.env
    )

    def run(command, **kwargs):
        if "features" in command:
            return SimpleNamespace(
                stdout="shell_tool\nunified_exec\napps\nplugins\nmulti_agent\nview_image\nshell_snapshot\nskill_search\nskip_host_skill_discovery",
                stderr="",
                returncode=0,
            )
        if "--help" in command:
            return SimpleNamespace(
                stdout="--ignore-user-config --ignore-rules --output-schema --ephemeral --json",
                stderr="",
                returncode=0,
            )
        return SimpleNamespace(
            stdout="Logged in using ChatGPT", stderr="", returncode=0
        )

    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(
        "app.modules.library.publisher_codex.estimate_prompt_tokens", lambda *_: 101
    )
    with pytest.raises(RuntimeError, match="context budget"):
        adapter.prepare("x" * 101)


def test_separation_survives_keep_rename_alias_addition_and_unrelated_apply(
    test_client,
):
    from app.modules.library.publisher_merge_review import (
        review_action,
        save_draft,
        discard_draft,
    )
    from app.modules.library.publisher_workbench import apply_publishers

    _, app = test_client
    db = app.state.db
    rows = [
        {
            "key": f"raw:{name}",
            "canonical_id": None,
            "raw_name": name,
            "display_name": name,
            "aliases": [],
            "is_new": True,
            "document_count": 1,
        }
        for name in ["One", "Two", "Three"]
    ]
    aid = db.create_publisher_analysis(rows, inventory_fingerprint(rows), "all", {})
    groups = proposal(["raw:One", "raw:Two", "raw:Three"])["clusters"]
    groups[0]["proposed_name"] = "One"
    db.checkpoint_publisher_analysis(aid, groups, {})
    db.import_publisher_analysis(aid)
    pid = db.get_publisher_review()["proposals"][0]["proposal_id"]
    review_action(db, pid, {"action": "separate", "member_ids": ["raw:One", "raw:Two"]})
    db.apply_publisher_change_set(
        {"renames": [], "keeps": ["One", "Two"], "merges": []}
    )
    canonicals = {
        row["display_name"]: row["canonical_id"]
        for row in db.list_normalization_canonicals("publisher")
    }
    expected_pair = sorted(
        [f"canonical:{canonicals['One']}", f"canonical:{canonicals['Two']}"]
    )
    assert db.publisher_analysis_state()["separations"] == [expected_pair]
    changes = {
        "renames": [{"canonical_id": canonicals["One"], "display_name": "Renamed"}]
    }
    save_draft(
        db, {"revision": db.get_publisher_review()["revision"], "changes": changes}
    )
    # A catalog change outside the reviewed identity must not invalidate the draft.
    db.apply_publisher_change_set({"renames": [], "keeps": ["Unrelated"], "merges": []})
    apply_publishers(
        db, {"use_draft": True, "revision": db.get_publisher_review()["revision"]}
    )
    assert db.publisher_analysis_state()["separations"] == [expected_pair]
    db.apply_publisher_change_set(
        {
            "renames": [],
            "keeps": [],
            "merges": [
                {
                    "canonical_ids": [canonicals["One"]],
                    "raw_names": ["O."],
                    "display_name": "Renamed",
                }
            ],
        }
    )
    assert db.publisher_analysis_state()["separations"] == [expected_pair]
    rev = db.get_publisher_review()["revision"]
    save_draft(db, {"revision": rev, "changes": {"keeps": ["Discarded"]}})
    discard_draft(db, {"revision": db.get_publisher_review()["revision"]})
    assert db.get_publisher_review()["draft"]["keeps"] == []
    assert not any(
        row["display_name"] == "Discarded"
        for row in db.list_normalization_canonicals("publisher")
    )


def test_edited_group_discard_restores_proposal_and_rerun_preserves_draft(test_client):
    from app.modules.library.publisher_merge_review import review_action, discard_draft

    _, app = test_client
    db = app.state.db
    rows = [
        {
            "key": f"raw:{name}",
            "canonical_id": None,
            "raw_name": name,
            "display_name": name,
            "aliases": [],
            "is_new": True,
            "document_count": 1,
        }
        for name in ["One", "Two", "Three"]
    ]
    groups = proposal(["raw:One", "raw:Two", "raw:Three"])["clusters"]
    groups[0]["proposed_name"] = "One"

    def import_groups():
        aid = db.create_publisher_analysis(rows, inventory_fingerprint(rows), "all", {})
        db.checkpoint_publisher_analysis(aid, groups, {})
        return db.import_publisher_analysis(aid)

    assert import_groups() == 1
    pid = db.get_publisher_review()["proposals"][0]["proposal_id"]
    review_action(
        db, pid, {"action": "skip", "member_ids": ["raw:One", "raw:Two", "raw:Three"]}
    )
    assert db.get_publisher_review()["proposals"][0]["status"] == "skipped"
    review_action(
        db,
        pid,
        {
            "action": "stage",
            "member_ids": ["raw:One", "raw:Two"],
            "display_name": "Owner choice",
        },
    )
    assert import_groups() == 0
    assert (
        db.get_publisher_review()["draft"]["merges"][0]["display_name"]
        == "Owner choice"
    )
    discard_draft(db, {"revision": db.get_publisher_review()["revision"]})
    assert db.get_publisher_review()["proposals"][0]["status"] == "pending"
    assert db.get_publisher_review()["draft"]["merges"] == []


@pytest.mark.parametrize(
    "mode,exception",
    [
        ("cancel", InterruptedError),
        ("timeout", TimeoutError),
        ("failure", RuntimeError),
    ],
)
def test_cli_cancellation_timeout_and_service_failure(
    monkeypatch, tmp_path, mode, exception
):
    from app.modules.library import publisher_codex

    adapter = publisher_codex.CodexAdapter(CodexSettings(timeout_seconds=1), tmp_path)
    stopped = []

    class Process:
        returncode = 1 if mode == "failure" else None

        def poll(self):
            return self.returncode

    def popen(command, **kwargs):
        assert (
            "--model" in command
            and command[command.index("--model") + 1] == "gpt-6.1-sol"
        )
        assert "features.shell_tool=false" in command
        assert kwargs["env"].get("OPENAI_API_KEY") is None
        return Process()

    monkeypatch.setattr(publisher_codex.subprocess, "Popen", popen)
    monkeypatch.setattr(
        publisher_codex, "stop_process", lambda process: stopped.append(True)
    )
    clock = iter([0, 5])
    monkeypatch.setattr(publisher_codex.time, "monotonic", lambda: next(clock))
    with pytest.raises(exception):
        adapter.analyze("Complete inventory", lambda: mode == "cancel")
    assert stopped == [True]
    assert not (tmp_path / "response.json").exists()


def test_cli_lifecycle_usage_and_missing_usage(monkeypatch, tmp_path):
    import json
    from app.modules.library import publisher_codex

    adapter = publisher_codex.CodexAdapter(CodexSettings(), tmp_path)

    class Process:
        returncode = 0

        def poll(self):
            return 0

    def popen(command, **kwargs):
        kwargs["stdout"].write((json.dumps({"type": "turn.completed"}) + "\n").encode())
        path = command[command.index("--output-last-message") + 1]
        from pathlib import Path

        Path(path).write_text('{"clusters": []}')
        return Process()

    monkeypatch.setattr(publisher_codex.subprocess, "Popen", popen)
    monkeypatch.setattr(publisher_codex, "stop_process", lambda _: None)
    response, usage = adapter.analyze("Complete inventory", lambda: False)
    assert response == {"clusters": []}
    assert usage["reported_token_usage"] is None and usage["reported_model"] is None


def test_api_durable_draft_routes_and_strict_apply(test_client):
    client, _ = test_client
    payload = client.get("/api/library/publishers/merge-suggestions").json()
    assert payload["draft"]["merges"] == []
    assert (
        client.put(
            "/api/library/publishers/draft", json={"revision": False, "changes": {}}
        ).status_code
        == 400
    )
    response = client.put(
        "/api/library/publishers/draft",
        json={"revision": payload["revision"], "changes": {"keeps": ["Review Press"]}},
    )
    assert response.status_code == 200
    assert client.get("/api/library/publishers/merge-suggestions").json()["draft"][
        "keeps"
    ] == ["Review Press"]
    rev = response.json()["revision"]
    assert (
        client.post(
            "/api/library/publishers/change-set/apply",
            json={"use_draft": True, "revision": rev - 1},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/library/publishers/change-set/apply",
            json={"use_draft": True, "revision": rev},
        ).status_code
        == 200
    )


def test_manual_draft_rejects_stale_review_snapshot(test_client):
    from app.modules.library.publisher_merge_review import save_draft

    _, app = test_client
    db = app.state.db
    db.apply_publisher_change_set({"renames": [], "keeps": ["Before"], "merges": []})
    cid = db.list_normalization_canonicals("publisher")[0]["canonical_id"]
    db.rename_normalization_canonical("publisher", cid, "After", "after")
    with pytest.raises(ValueError, match="refresh"):
        save_draft(
            db,
            {
                "revision": 0,
                "changes": {
                    "renames": [{"canonical_id": cid, "display_name": "Owner edit"}]
                },
                "reviewed": {
                    f"canonical:{cid}": {
                        "display_name": "Before",
                        "aliases": ["Before"],
                        "is_new": False,
                    }
                },
            },
        )
    assert db.get_publisher_review()["draft"]["renames"] == []


def test_new_scope_generation_is_one_session_and_checkpointed_before_import(
    inventory, monkeypatch, tmp_path
):
    from app.modules.library.runtime import run_suggest_publisher_merges as runner

    calls = []

    class DB:
        state = {"successful": True, "checkpoint": None, "separations": []}

        def publisher_analysis_state(self):
            return self.state

        def create_publisher_analysis(self, *args):
            calls.append(("create", args[2]))
            return 3

        def checkpoint_publisher_analysis(self, *args):
            assert (
                next(cluster for cluster in args[1] if cluster["kind"] == "cluster")
                == proposal()["clusters"][0]
            )
            assert next(
                cluster for cluster in args[1] if cluster["kind"] == "singleton"
            )["member_ids"] == ["canonical:8"]
            calls.append(("checkpoint", args[0]))

        def import_publisher_analysis(self, aid):
            calls.append(("import", aid))
            return 1

    class Adapter:
        version = "mock"

        def __init__(self, *args):
            pass

        def prepare(self, prompt):
            assert '["3","c",["Other"]]' in prompt  # Full catalog in new scope.
            return {}

        def telemetry(self):
            return None

        def analyze(self, prompt, should_stop):
            calls.append(("analyze",))
            payload = proposal(["1", "2"])
            return {
                "clusters": payload["clusters"][:1],
                "singleton_ids": ["3"],
                "unresolved_ids": [],
            }, {"reported_token_usage": None}

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(runner, "build_inventory", lambda db: inventory)
    result = runner.run_analysis(
        db=DB(),
        settings=CodexSettings(),
        workspace=tmp_path,
        should_stop=lambda: False,
        adapter_factory=Adapter,
    )
    assert calls == [
        ("create", "new"),
        ("analyze",),
        ("checkpoint", 3),
        ("import", 3),
        ("close",),
    ]
    assert result["quota_observations"]["available"] is False


def test_quota_multiple_buckets_duration_not_field_position():
    before = {
        "rateLimitsByLimitId": {
            "codex": {
                "secondary": {
                    "usedPercent": 10,
                    "windowDurationMins": 300,
                    "resetsAt": 100,
                }
            },
            "research": {
                "primary": {
                    "usedPercent": 20,
                    "windowDurationMins": 10080,
                    "resetsAt": 200,
                }
            },
        }
    }
    after = {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {
                    "usedPercent": 13,
                    "windowDurationMins": 300,
                    "resetsAt": 100,
                }
            },
            "research": {
                "secondary": {
                    "usedPercent": 21,
                    "windowDurationMins": 10080,
                    "resetsAt": 200,
                }
            },
        }
    }
    rows = quota_observations(before, after)["windows"]
    assert [(row["label"], row["delta_percentage_points"]) for row in rows] == [
        ("Five-hour", 3),
        ("Weekly", 1),
    ]


@pytest.mark.parametrize("mode", ["missing", "api_key"])
def test_subscription_preflight_never_falls_back(monkeypatch, tmp_path, mode):
    from types import SimpleNamespace
    from app.modules.library.publisher_codex import CodexAdapter
    from app.modules.library import publisher_codex

    adapter = CodexAdapter(CodexSettings(), tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if mode == "missing":
            raise FileNotFoundError("codex")
        if "--help" in command:
            stdout = (
                "--ignore-user-config --ignore-rules --output-schema --ephemeral --json"
            )
        elif "features" in command:
            stdout = "shell_tool\nunified_exec\napps\nplugins\nmulti_agent\nview_image\nshell_snapshot\nskill_search\nskip_host_skill_discovery"
        else:
            stdout = "Logged in using an API key"
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr(publisher_codex.subprocess, "run", run)
    with pytest.raises(
        RuntimeError,
        match="missing" if mode == "missing" else "subscription authentication",
    ):
        adapter.prepare("Inventory")
    assert all("exec" not in command or "--help" in command for command in calls)


def test_import_conflicting_separation_rolls_back_and_apply_alias_conflict_retains_draft(
    test_client,
):
    from app.modules.library.publisher_merge_review import save_draft
    from app.modules.library.publisher_workbench import apply_publishers

    _, app = test_client
    db = app.state.db
    rows = [
        {
            "key": f"raw:{name}",
            "canonical_id": None,
            "raw_name": name,
            "display_name": name,
            "aliases": [],
            "is_new": True,
            "document_count": 1,
        }
        for name in ["One", "Two"]
    ]
    groups = proposal(["raw:One", "raw:Two"])["clusters"]
    groups[0]["proposed_name"] = "One"
    aid = db.create_publisher_analysis(rows, inventory_fingerprint(rows), "all", {})
    db.checkpoint_publisher_analysis(aid, groups, {})
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO publisher_separations VALUES ('raw:One','raw:Two','{}'::jsonb,'now')"
        )
    with pytest.raises(ValueError, match="separation"):
        db.import_publisher_analysis(aid)
    assert db.get_publisher_review()["proposals"] == []
    assert db.publisher_analysis_state()["checkpoint"]["analysis_id"] == aid
    db.apply_publisher_change_set(
        {"keeps": ["Target", "Taken"], "renames": [], "merges": []}
    )
    canonicals = {
        row["display_name"]: row["canonical_id"]
        for row in db.list_normalization_canonicals("publisher")
    }
    save_draft(
        db,
        {
            "revision": db.get_publisher_review()["revision"],
            "changes": {
                "renames": [
                    {"canonical_id": canonicals["Target"], "display_name": "Taken"}
                ]
            },
        },
    )
    with pytest.raises(ValueError, match="already linked"):
        apply_publishers(
            db, {"use_draft": True, "revision": db.get_publisher_review()["revision"]}
        )
    assert (
        db.get_normalization_canonical(canonicals["Target"])["display_name"] == "Target"
    )
    assert len(db.get_publisher_review()["draft"]["renames"]) == 1


def test_unstaged_proposal_edits_survive_reload_and_rerun(test_client):
    from app.modules.library.publisher_merge_review import review_action

    _, app = test_client
    db = app.state.db
    rows = [
        {
            "key": f"raw:{name}",
            "canonical_id": None,
            "raw_name": name,
            "display_name": name,
            "aliases": [],
            "is_new": True,
            "document_count": 1,
        }
        for name in ["One", "Two", "Three"]
    ]
    aid = db.create_publisher_analysis(rows, inventory_fingerprint(rows), "all", {})
    groups = proposal(["raw:One", "raw:Two", "raw:Three"])["clusters"]
    groups[0]["proposed_name"] = "One"
    db.checkpoint_publisher_analysis(aid, groups, {})
    db.import_publisher_analysis(aid)
    pid = db.get_publisher_review()["proposals"][0]["proposal_id"]
    review_action(
        db,
        pid,
        {
            "action": "edit",
            "member_ids": ["raw:One", "raw:Two"],
            "display_name": "Owner name",
        },
    )
    stored = db.get_publisher_review()["proposals"][0]
    assert stored["review_edit"] == {
        "member_ids": ["raw:One", "raw:Two"],
        "display_name": "Owner name",
    }
    assert stored["status"] == "pending"
    assert db.get_publisher_review()["draft"]["merges"] == []


def test_skip_retains_immediate_edits(test_client):
    from app.modules.library.publisher_merge_review import review_action

    _, app = test_client
    db = app.state.db
    rows = [
        {
            "key": f"raw:{name}",
            "canonical_id": None,
            "raw_name": name,
            "display_name": name,
            "aliases": [],
            "is_new": True,
            "document_count": 1,
        }
        for name in ["One", "Two", "Three"]
    ]
    aid = db.create_publisher_analysis(rows, inventory_fingerprint(rows), "all", {})
    groups = proposal(["raw:One", "raw:Two", "raw:Three"])["clusters"]
    groups[0]["proposed_name"] = "One"
    db.checkpoint_publisher_analysis(aid, groups, {})
    db.import_publisher_analysis(aid)
    pid = db.get_publisher_review()["proposals"][0]["proposal_id"]
    review_action(
        db,
        pid,
        {
            "action": "skip",
            "member_ids": ["raw:One", "raw:Two"],
            "display_name": "Immediate edit",
        },
    )
    assert (
        db.get_publisher_review()["proposals"][0]["review_edit"]["display_name"]
        == "Immediate edit"
    )


def test_publisher_runner_supports_task_runtime_single_connection_setting(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    from contextlib import contextmanager
    from app.modules.library.runtime import run_suggest_publisher_merges as runner

    created = []

    class DB:
        def __init__(self, *args, **kwargs):
            created.append(kwargs)
            assert kwargs.get("pool_size", 1) >= 2, (
                "advisory lock must leave a query connection available"
            )

        @contextmanager
        def publisher_analysis_lock(self):
            yield

        def close(self):
            pass

    monkeypatch.setenv("MANZARA_DB_POOL_SIZE", "1")
    monkeypatch.setattr(
        runner,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://test",
            database_schema="test",
            local_state_path=tmp_path / "runtime.sqlite3",
            database_pool_size=1,
        ),
    )
    monkeypatch.setattr(runner, "Database", DB)
    monkeypatch.setattr(runner, "workspace_dir", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(
        runner,
        "run_analysis",
        lambda **kwargs: {"kind": "library.publisher_merge_summary"},
    )
    monkeypatch.setattr(runner, "emit_run_artifact", lambda _: True)
    monkeypatch.setattr(runner.signal, "signal", lambda *_: None)
    monkeypatch.setattr(runner.sys, "argv", ["run_suggest_publisher_merges"])
    runner.main()
    assert created[0]["pool_size"] == 2


def test_publisher_analysis_lock_leaves_query_capacity_and_releases_after_failure(
    test_client, tmp_path
):
    from app.db import Database

    _, app = test_client
    existing = app.state.db
    # Separate schema uses an independent pool in the same disposable test database.
    schema = existing.schema + "_publisher_lock"
    db = Database(
        existing.database_url,
        schema=schema,
        pool_size=2,
        local_state_path=tmp_path / "lock.sqlite3",
    )
    try:
        with db.publisher_analysis_lock():
            with db._connect() as conn:
                assert conn.execute("SELECT 1 AS value").fetchone()["value"] == 1
                assert (
                    conn.execute(
                        "SELECT pg_try_advisory_lock(726193450) AS acquired"
                    ).fetchone()["acquired"]
                    is False
                )
        with pytest.raises(RuntimeError, match="simulated"):
            with db.publisher_analysis_lock():
                raise RuntimeError("simulated")
        with db.publisher_analysis_lock():
            with db._connect() as conn:
                assert conn.execute("SELECT 1 AS value").fetchone()["value"] == 1
    finally:
        db.close()


def test_compact_prompt_retains_all_names_counts_and_separation_ids(inventory):
    import json

    original = deepcopy(inventory)
    inventory[1]["aliases"] = ["TK", "TK"]
    prompt = build_prompt(inventory, "new", [["canonical:7", "raw:TK"]])
    rows = json.loads(
        prompt.split("Complete inventory: ", 1)[1].split("\nRun settings:", 1)[0]
    )
    assert rows == [
        ["1", "c", ["Татар китап", "Tatar kitap"]],
        ["2", "u", ["TK"]],
        ["3", "c", ["Other"]],
    ]
    assert 'Recorded pairwise separation decisions: [["1","2"]]' in prompt
    assert "canonical:7" not in prompt and "raw:TK" not in prompt
    assert prompt.count("Татар китап") == 1
    assert prompt.count("TK") == 1
    assert "distinct_document_counts=[3,2,1]" in prompt
    assert inventory[0] == original[0]  # Local durable inventory is untouched.
    assert prompt == build_prompt(inventory, "new", [["canonical:7", "raw:TK"]])


def test_compact_response_restores_durable_ids_and_rejects_unknown_ids(inventory):
    from app.modules.library.publisher_merge_contract import restore_response_ids

    response = {**proposal(["1", "2"]), "singleton_ids": [], "unresolved_ids": []}
    restored = restore_response_ids(response, inventory)
    assert response["clusters"][0]["member_ids"] == ["1", "2"]
    assert restored == proposal()
    validate_response(restored, inventory, "new", [])
    with pytest.raises(ValueError, match="separation"):
        validate_response(restored, inventory, "new", [["canonical:7", "raw:TK"]])
    for unknown in ("4", "canonical:7", "raw:TK"):
        with pytest.raises(ValueError, match="unknown"):
            restore_response_ids(
                {**proposal(["1", unknown]), "singleton_ids": [], "unresolved_ids": []},
                inventory,
            )


def test_prompt_cache_prefix_survives_scope_count_and_separation_changes(inventory):
    first = build_prompt(inventory, "all", [])
    changed = deepcopy(inventory)
    changed[0]["document_count"] = 100
    second = build_prompt(changed, "new", [["canonical:7", "raw:TK"]])
    assert first.split("\nRun settings:", 1)[0] == second.split("\nRun settings:", 1)[0]
    assert first != second
    assert "distinct_document_counts" in second and "100" in second


def test_cli_cache_context_is_stable_while_run_artifacts_remain_separate(
    monkeypatch, tmp_path
):
    import json
    from pathlib import Path
    from app.modules.library import publisher_codex

    contexts = []

    class Process:
        returncode = 0

        def poll(self):
            return 0

    def popen(command, **kwargs):
        context = Path(command[command.index("-C") + 1])
        assert kwargs["cwd"] == context
        assert context.is_dir() and not list(context.iterdir())
        contexts.append(context)
        kwargs["stdout"].write(
            (
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 20,
                            "cached_input_tokens": 17,
                            "output_tokens": 2,
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        Path(command[command.index("--output-last-message") + 1]).write_text(
            '{"clusters":[]}'
        )
        return Process()

    monkeypatch.setattr(publisher_codex.subprocess, "Popen", popen)
    monkeypatch.setattr(publisher_codex, "stop_process", lambda _: None)
    for run in ("run-1", "run-2"):
        workspace = tmp_path / run
        workspace.mkdir()
        adapter = publisher_codex.CodexAdapter(CodexSettings(), workspace)
        try:
            _, usage = adapter.analyze("Complete inventory", lambda: False)
            assert usage["reported_token_usage"]["cached_input_tokens"] == 17
            assert (workspace / "response.json").exists()
            assert (workspace / "lifecycle.jsonl").exists()
        finally:
            adapter.close()
    assert contexts[0] == contexts[1]


def test_overlapping_groups_are_retained_for_owner_review(inventory):
    payload = proposal()
    other = deepcopy(payload["clusters"][0])
    other.update(member_ids=["raw:TK", "canonical:8"], proposed_name="Other")
    payload["clusters"].append(other)
    groups = validate_response(payload, inventory, "all", [])
    assert len(groups) == 2
    assert groups[0]["member_ids"] == ["canonical:7", "raw:TK"]
    assert groups[1]["member_ids"] == ["raw:TK", "canonical:8"]


def test_overlap_review_links_and_staging_require_disjoint_owner_choices(test_client):
    from app.modules.library.publisher_merge_review import review_action

    _, app = test_client
    db = app.state.db
    rows = [
        {
            "key": f"raw:{name}",
            "display_name": name,
            "aliases": [],
            "is_new": True,
            "canonical_id": None,
            "raw_name": name,
            "document_count": 1,
        }
        for name in ("One", "Two", "Three")
    ]
    response = proposal(["raw:One", "raw:Two"])
    response["clusters"][0]["proposed_name"] = "One"
    other = deepcopy(response["clusters"][0])
    other.update(member_ids=["raw:Two", "raw:Three"], proposed_name="Three")
    response["clusters"].append(other)
    aid = db.create_publisher_analysis(rows, inventory_fingerprint(rows), "all", {})
    db.checkpoint_publisher_analysis(
        aid, validate_response(response, rows, "all", []), {}
    )
    assert db.import_publisher_analysis(aid) == 2
    proposals = db.get_publisher_review()["proposals"]
    first, second = proposals
    assert first["conflict_member_ids"] == ["raw:Two"]
    assert first["conflicting_proposal_ids"] == [second["proposal_id"]]
    assert second["conflicting_proposal_ids"] == [first["proposal_id"]]
    review_action(
        db,
        first["proposal_id"],
        {
            "action": "stage",
            "member_ids": ["raw:One", "raw:Two"],
            "display_name": "One",
        },
    )
    with pytest.raises(ValueError, match="already staged"):
        review_action(
            db,
            second["proposal_id"],
            {
                "action": "stage",
                "member_ids": ["raw:Two", "raw:Three"],
                "display_name": "Three",
            },
        )
    review = review_action(
        db,
        second["proposal_id"],
        {"action": "edit", "member_ids": ["raw:Three"], "display_name": "Three"},
    )
    assert all(not p["conflict_member_ids"] for p in review["proposals"])


def test_saved_completed_response_recovery_imports_without_adapter(
    test_client, tmp_path
):
    import json
    from app.modules.library.runtime import run_suggest_publisher_merges as runner

    _, app = test_client
    db = app.state.db
    rows = [
        {
            "key": f"raw:{name}",
            "display_name": name,
            "aliases": [],
            "is_new": True,
            "canonical_id": None,
            "raw_name": name,
            "document_count": 1,
        }
        for name in ("One", "Two", "Three")
    ]
    fingerprint = inventory_fingerprint(rows)
    aid = db.create_publisher_analysis(
        rows, fingerprint, "all", {"prompt_version": "publisher-clusters.v3"}
    )
    payload = {**proposal(["1", "2"]), "singleton_ids": [], "unresolved_ids": []}
    payload["clusters"][0]["proposed_name"] = "One"
    other = deepcopy(payload["clusters"][0])
    other.update(member_ids=["2", "3"], proposed_name="Three")
    payload["clusters"].append(other)
    (tmp_path / "inventory.json").write_text(
        json.dumps({"inventory": rows, "fingerprint": fingerprint})
    )
    (tmp_path / "response.json").write_text(json.dumps(payload))
    (tmp_path / "lifecycle.jsonl").write_text(
        json.dumps({"type": "turn.completed", "usage": {"cached_input_tokens": 17}})
        + "\n"
    )
    result = runner.recover_completed_response(
        db=db, analysis_id=aid, workspace=tmp_path
    )
    assert result["new_proposals"] == 2 and result["conflicting_groups"] == 2
    assert result["reported_token_usage"] == {"cached_input_tokens": 17}
    assert result["recovered"] is True
    assert db.get_publisher_analysis(aid)["state"] == "imported"
    assert (
        runner.recover_completed_response(db=db, analysis_id=aid, workspace=tmp_path)[
            "new_proposals"
        ]
        == 0
    )


@pytest.mark.parametrize("mismatch", ["fingerprint", "incomplete"])
def test_saved_response_recovery_rejects_mismatched_or_incomplete_artifacts(
    test_client, tmp_path, mismatch
):
    import json
    from app.modules.library.runtime import run_suggest_publisher_merges as runner

    _, app = test_client
    db = app.state.db
    rows = []
    fingerprint = inventory_fingerprint(rows)
    aid = db.create_publisher_analysis(
        rows, fingerprint, "all", {"prompt_version": "publisher-clusters.v3"}
    )
    (tmp_path / "inventory.json").write_text(
        json.dumps(
            {
                "inventory": rows,
                "fingerprint": "wrong" if mismatch == "fingerprint" else fingerprint,
            }
        )
    )
    (tmp_path / "response.json").write_text(
        '{"clusters":[],"singleton_ids":[],"unresolved_ids":[]}'
    )
    (tmp_path / "lifecycle.jsonl").write_text(
        json.dumps(
            {"type": "turn.failed" if mismatch == "incomplete" else "turn.completed"}
        )
        + "\n"
    )
    with pytest.raises(ValueError):
        runner.recover_completed_response(db=db, analysis_id=aid, workspace=tmp_path)
    assert db.get_publisher_analysis(aid)["state"] == "generating"
    assert not db.get_publisher_review()["proposals"]


def test_forward_migration_adds_review_edit_to_already_deployed_proposals(
    test_client, tmp_path
):
    from app.db import Database

    _, app = test_client
    schema = app.state.db.schema + "_review_edit_upgrade"
    db = Database(
        app.state.db.database_url,
        schema=schema,
        pool_size=1,
        local_state_path=tmp_path / "migration.sqlite3",
    )
    try:
        with db._connect() as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
            conn.execute(
                "CREATE TABLE alembic_version_manzara(version_num VARCHAR(32) PRIMARY KEY)"
            )
            conn.execute("INSERT INTO alembic_version_manzara VALUES ('20261001_0054')")
            conn.execute(
                "CREATE TABLE publisher_merge_proposals(proposal_id BIGINT PRIMARY KEY,proposal JSONB NOT NULL)"
            )
            conn.execute(
                'INSERT INTO publisher_merge_proposals VALUES (1,\'{"proposed_name":"Preserved"}\'::jsonb)'
            )
        db.init_schema()
        with db._connect() as conn:
            row = conn.execute(
                "SELECT proposal,review_edit FROM publisher_merge_proposals"
            ).fetchone()
        assert (
            row["proposal"] == {"proposed_name": "Preserved"}
            and row["review_edit"] is None
        )
        db.init_schema()
    finally:
        with db._connect() as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        db.close()


def test_complete_clustering_accounts_for_every_entry_and_rejects_old_response(
    inventory,
):
    from app.modules.library.publisher_merge_contract import (
        validate_clustering_response,
    )

    payload = {
        "clusters": [
            dict(proposal()["clusters"][0], kind="cluster"),
            {
                "kind": "singleton",
                "member_ids": ["canonical:8"],
                "proposed_name": "Other",
                "rationale": "Established publisher.",
                "confidence": "strong",
                "uncertainty": "",
                "citations": [],
            },
        ]
    }
    result = validate_clustering_response(payload, inventory, "all", [])
    assert len(result) == 2
    with pytest.raises(ValueError, match="account"):
        validate_clustering_response(
            {"clusters": payload["clusters"][:1]}, inventory, "all", []
        )
    with pytest.raises(ValueError):
        validate_clustering_response(
            {"groups": payload["clusters"]}, inventory, "all", []
        )
    payload["clusters"][0]["proposed_name"] = "General publisher name"
    assert any(
        cluster["proposed_name"] == "General publisher name"
        for cluster in validate_clustering_response(payload, inventory, "all", [])
    )


def test_cluster_review_exposes_proposed_aliases_and_complete_coverage(
    test_client, inventory
):
    _, app = test_client
    db = app.state.db
    clusters = [
        dict(proposal()["clusters"][0], kind="cluster"),
        {
            "kind": "unresolved",
            "member_ids": ["canonical:8"],
            "proposed_name": "Other",
            "rationale": "Insufficient evidence.",
            "confidence": "uncertain",
            "uncertainty": "Needs review.",
            "citations": [],
        },
    ]
    aid = db.create_publisher_analysis(
        inventory, inventory_fingerprint(inventory), "all", {}
    )
    db.checkpoint_publisher_analysis(aid, clusters, {})
    db.import_publisher_analysis(aid)
    review = db.get_publisher_review()
    assert review["coverage"]["inventory_entries"] == 3
    assert review["coverage"]["covered_entries"] == 3
    assert review["coverage"]["unresolved_entries"] == 1
    assert review["proposals"][0]["proposed_aliases"] == ["TK", "Tatar kitap"]


def test_singleton_cluster_stages_as_keep_without_merging(test_client):
    from app.modules.library.publisher_merge_review import review_action

    _, app = test_client
    db = app.state.db
    row = {
        "key": "raw:Alone",
        "display_name": "Alone",
        "aliases": [],
        "canonical_id": None,
        "raw_name": "Alone",
        "is_new": True,
        "document_count": 1,
    }
    cluster = {
        "kind": "singleton",
        "member_ids": ["raw:Alone"],
        "proposed_name": "Alone",
        "rationale": "Standalone identity.",
        "confidence": "strong",
        "uncertainty": "",
        "citations": [],
    }
    aid = db.create_publisher_analysis([row], inventory_fingerprint([row]), "all", {})
    db.checkpoint_publisher_analysis(aid, [cluster], {})
    db.import_publisher_analysis(aid)
    pid = db.get_publisher_review()["proposals"][0]["proposal_id"]
    result = review_action(
        db,
        pid,
        {"action": "stage", "member_ids": ["raw:Alone"], "display_name": "Alone"},
    )
    assert result["draft"]["keeps"] == ["Alone"] and result["draft"]["merges"] == []
    with pytest.raises(ValueError, match="staged"):
        db.save_publisher_draft(
            {"keeps": [], "renames": [], "merges": []}, result["revision"]
        )


def test_discarding_previous_analysis_preserves_manual_draft_and_separations(
    test_client,
):
    _, app = test_client
    db = app.state.db
    rows = [
        {
            "key": f"raw:{name}",
            "display_name": name,
            "aliases": [],
            "canonical_id": None,
            "raw_name": name,
            "is_new": True,
            "document_count": 1,
        }
        for name in ("One", "Two")
    ]
    group = proposal(["raw:One", "raw:Two"])["clusters"][0]
    group["proposed_name"] = "One"
    aid = db.create_publisher_analysis(rows, inventory_fingerprint(rows), "all", {})
    db.checkpoint_publisher_analysis(aid, [group], {})
    db.import_publisher_analysis(aid)
    review = db.get_publisher_review()
    db.save_publisher_draft(
        {"keeps": ["Manual"], "renames": [], "merges": []}, review["revision"]
    )
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO publisher_separations VALUES ('raw:A','raw:B','{}'::jsonb,'now')"
        )
    db.discard_publisher_analyses()
    review = db.get_publisher_review()
    assert not review["proposals"] and review["draft"]["keeps"] == ["Manual"]
    state = db.publisher_analysis_state()
    assert state["successful"] is False and state["separations"] == [["raw:A", "raw:B"]]


def test_compact_singleton_output_is_explicit_complete_and_has_no_missing_id_fallback(
    inventory,
):
    from app.modules.library.publisher_merge_contract import (
        restore_response_ids,
        validate_clustering_response,
    )

    wire = {"clusters": [], "singleton_ids": ["1"], "unresolved_ids": ["2", "3"]}
    result = validate_clustering_response(
        restore_response_ids(wire, inventory), inventory, "all", []
    )
    assert len(result) == 3 and {c["member_ids"][0] for c in result} == {
        r["key"] for r in inventory
    }
    assert (
        next(c for c in result if c["kind"] == "singleton")["proposed_name"]
        == inventory[0]["display_name"]
    )
    with pytest.raises(ValueError):
        restore_response_ids({"clusters": []}, inventory)
    with pytest.raises(ValueError, match="account"):
        validate_clustering_response(
            restore_response_ids({**wire, "unresolved_ids": ["2"]}, inventory),
            inventory,
            "all",
            [],
        )
    with pytest.raises(ValueError, match="unknown"):
        restore_response_ids({**wire, "singleton_ids": ["invalid"]}, inventory)


def test_explicit_unresolved_assignment_preserves_long_source_names(inventory):
    from app.modules.library.publisher_merge_contract import (
        restore_response_ids,
        validate_clustering_response,
    )

    inventory[1]["display_name"] = "Long source entry " * 30
    wire = {"clusters": [], "singleton_ids": ["1", "3"], "unresolved_ids": ["2"]}
    result = validate_clustering_response(
        restore_response_ids(wire, inventory), inventory, "all", []
    )
    assert (
        next(c for c in result if c["kind"] == "unresolved")["proposed_name"]
        == inventory[1]["display_name"]
    )
