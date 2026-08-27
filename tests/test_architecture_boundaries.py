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
        "shayan",
    ):
        assert (ROOT / "app" / "repositories" / f"{module}.py").is_file()
        assert f"app.repositories.{module}" in source


def test_flow_specific_agent_guidance_is_nested():
    root_guidance = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Shayan video archive policy" not in root_guidance
    assert "Library metadata extraction" not in root_guidance
    assert "Document storage policy" not in root_guidance
    for flow in ("library", "maintenance", "shayan"):
        assert (ROOT / "app" / "modules" / flow / "AGENTS.md").is_file()


def test_architecture_index_names_major_ownership_boundaries():
    source = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    for concern in ("Database repositories", "Task runtime", "Frontend", "Flow modules"):
        assert concern in source


def test_task_runtime_helpers_are_outside_task_runner_module():
    for module in ("commands.py", "logging.py", "process.py"):
        assert (ROOT / "app" / "task_runtime" / module).is_file()


def test_large_normalization_page_loads_domain_scripts_before_controller():
    html = (ROOT / "static" / "library-normalization.html").read_text(encoding="utf-8")
    rendering = html.index('/static/library-normalization-rendering.js')
    controller = html.index('/static/library-normalization.js')
    assert rendering < controller
