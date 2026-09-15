import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _assert_acyclic(dependencies):
    def visit(module, ancestors):
        assert module not in ancestors, (
            f"Circular owner imports: {ancestors + (module,)}"
        )
        for dependency in dependencies.get(module, ()):
            visit(dependency, ancestors + (module,))

    for module in dependencies:
        visit(module, ())


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
    for concern in (
        "Database repositories",
        "Task runtime",
        "Frontend",
        "Library flow",
    ):
        assert concern in source


@pytest.mark.parametrize(
    ("entrypoint", "modules"),
    [
        (
            "normalization",
            (
                "rules",
                "queries",
                "views",
                "canonicals",
                "decisions",
                "history",
                "quality",
            ),
        ),
        (
            "non_pdf_extraction",
            ("types", "formats", "converters", "media", "pandoc", "rendering"),
        ),
    ],
)
def test_library_helpers_have_focused_owners_without_importing_entrypoints(
    entrypoint, modules
):
    library = ROOT / "app" / "modules" / "library"
    prefix = "normalization" if entrypoint == "normalization" else "non_pdf"
    assert len((library / f"{entrypoint}.py").read_text().splitlines()) <= 250
    dependencies = {}
    for suffix in modules:
        path = library / f"{prefix}_{suffix}.py"
        source = path.read_text()
        assert len(source.splitlines()) <= 350, path
        dependencies[path.stem] = set()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != f"app.modules.library.{entrypoint}", path
                if node.module == "app.modules.library":
                    assert entrypoint not in {alias.name for alias in node.names}, path
                if node.module and node.module.startswith(
                    f"app.modules.library.{prefix}_"
                ):
                    dependencies[path.stem].add(node.module.rsplit(".", 1)[-1])

    _assert_acyclic(dependencies)


def test_library_navigation_routes_to_existing_files_and_focused_tests():
    index = ROOT / "app" / "modules" / "library" / "guidance" / "navigation.md"
    source = index.read_text()
    for document in (index, index.with_name("evaluation-navigation.md")):
        for token in re.findall(r"`([^`]+)`", document.read_text()):
            if token.endswith((".py", ".md", ".mjs")):
                assert (ROOT / token).is_file(), token
    for owner in (
        "normalization_queries",
        "normalization_decisions",
        "non_pdf_media",
        "non_pdf_rendering",
    ):
        assert f"{owner}.py" in source
    guidance = (index.parents[1] / "AGENTS.md").read_text()
    assert "guidance/navigation.md" in guidance


def test_shared_navigation_paths_exist():
    source = (ROOT / "docs" / "architecture.md").read_text()
    for token in re.findall(r"`([^`]+)`", source):
        if token.endswith((".py", ".md", ".mjs")) and "*" not in token:
            assert (ROOT / token).is_file(), token


def test_non_pdf_tests_follow_implementation_responsibilities():
    assert not (ROOT / "tests" / "test_library_non_pdf_extraction.py").exists()
    for owner in (
        "formats",
        "converters",
        "media",
        "rendering",
        "repository",
        "runtime",
        "contracts",
    ):
        path = ROOT / "tests" / f"test_library_non_pdf_{owner}.py"
        assert path.is_file(), path
        assert len(path.read_text().splitlines()) <= 450, path


def test_main_uses_injected_services_instead_of_repeating_operation_maps():
    tree = ast.parse((ROOT / "app" / "main.py").read_text())
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert not names.intersection(
        {
            "_payload_builder_operations",
            "_normalization_operations",
            "_classification_operations",
            "_entities_operations",
        }
    )
    source = (ROOT / "app" / "main.py").read_text()
    assert "build_application_operations" in source
    assert "state.operations.normalization" in source


def test_evaluation_owners_have_bounded_size_and_no_runtime_path_imports():
    metadata = ROOT / "app" / "modules" / "library" / "runtime" / "metadata"
    dependencies = {}
    for owner in (
        "types",
        "progress",
        "channel",
        "selection",
        "response",
        "request",
        "documents",
        "persistence",
        "worker",
        "text",
        "patch",
        "terms",
        "classification",
    ):
        path = metadata / f"evaluation_{owner}.py"
        assert path.is_file(), path
        source = path.read_text()
        assert len(source.splitlines()) <= 350, path
        dependencies[path.stem] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in {
                    "core",
                    "models",
                    "dirs",
                    "integrations",
                    "metadata",
                    "prompts",
                    "utils",
                }, path
                module = (node.module or "").rsplit(".", 1)[-1]
                assert module != "evaluation", path
                if node.module is None:
                    assert "evaluation" not in {alias.name for alias in node.names}, (
                        path
                    )
                if module.startswith("evaluation_"):
                    dependencies[path.stem].add(module)
    _assert_acyclic(dependencies)
    assert len((metadata / "evaluation.py").read_text().splitlines()) <= 200


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
    rendering = html.index("/static/library-normalization-rendering.js")
    controller = html.index("/static/library-normalization.js")
    assert rendering < controller


def test_legacy_hidden_monocorpus_workspace_is_not_referenced():
    legacy_path = "~/" + ".monocorpus"
    checked = [
        ROOT / "README.md",
        ROOT / "config.example.yaml",
        ROOT / "app" / "modules" / "runtime_shared_utils.py",
        ROOT / "app" / "modules" / "library" / "AGENTS.md",
        ROOT / "app" / "modules" / "library" / "runtime" / "dirs.py",
        ROOT / "app" / "modules" / "library" / "runtime" / "run_collection_validate.py",
    ]
    for path in checked:
        assert legacy_path not in path.read_text(encoding="utf-8"), path
