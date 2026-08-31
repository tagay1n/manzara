from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_database_public_facade_is_composed_from_focused_repositories():
    source = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
    assert "class Database(" in source
    for module in (
        "definitions",
        "gemini",
        "normalization",
        "runs",
    ):
        assert (ROOT / "app" / "repositories" / f"{module}.py").is_file()
        assert f"app.repositories.{module}" in source


def test_flow_specific_agent_guidance_is_nested():
    root_guidance = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert len(root_guidance.split()) <= 500
    assert "PDF previews" not in root_guidance
    assert "Pacific reset" not in root_guidance
    for flow in ("library", "maintenance"):
        guidance = ROOT / "app" / "modules" / flow / "AGENTS.md"
        assert guidance.is_file()
        assert len(guidance.read_text(encoding="utf-8").split()) <= 250

    assert (ROOT / "static" / "AGENTS.md").is_file()
    assert (ROOT / "app" / "task_runtime" / "AGENTS.md").is_file()
    assert (ROOT / "docs" / "gemini-runtime.md").is_file()


def test_architecture_index_names_major_ownership_boundaries():
    source = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    for concern in ("Database repositories", "Task runtime", "Frontend", "Library flow"):
        assert concern in source


def test_verification_docs_do_not_embed_ephemeral_results():
    source = (ROOT / "docs" / "verification.md").read_text(encoding="utf-8")
    assert "passed" not in source
    assert "current head" not in source
    assert "alembic heads" in source


def test_task_runtime_helpers_are_outside_task_runner_module():
    for module in ("commands.py", "logging.py", "process.py"):
        assert (ROOT / "app" / "task_runtime" / module).is_file()


def test_large_normalization_page_loads_domain_scripts_before_controller():
    html = (ROOT / "static" / "library-normalization.html").read_text(encoding="utf-8")
    rendering = html.index('/static/library-normalization-rendering.js')
    controller = html.index('/static/library-normalization.js')
    assert rendering < controller


def test_legacy_hidden_monocorpus_workspace_is_not_referenced():
    legacy_path = "~/" + ".monocorpus"
    checked = [
        ROOT / "README.md",
        ROOT / "config.example.yaml",
        ROOT / "app" / "modules" / "runtime_shared_utils.py",
        ROOT / "app" / "modules" / "library" / "AGENTS.md",
        ROOT / "app" / "modules" / "library" / "runtime" / "dirs.py",
        ROOT
        / "app"
        / "modules"
        / "library"
        / "runtime"
        / "run_collection_validate.py",
    ]
    for path in checked:
        assert legacy_path not in path.read_text(encoding="utf-8"), path
