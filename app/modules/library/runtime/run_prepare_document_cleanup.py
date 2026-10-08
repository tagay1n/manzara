"""Flow-owned cleanup preparation for the inline task runtime."""

from __future__ import annotations

from typing import Any, Mapping

from app.modules.library.document_cleanup_repository import DocumentCleanupRepository
from app.modules.library.document_cleanup_service import prepare_document_cleanup
from app.runtime_config import load_runtime_config
from app.settings import load_settings
from app.task_runtime.contracts import RunContext

TASK_ID = "library.prepare_document_cleanup"


def cleanup_paths() -> dict[str, str]:
    """Planning needs source paths, without requiring storage credentials."""
    config = load_runtime_config()
    value: Any = config
    for key in ("yandex", "disk", "documents"):
        value = value.get(key) if isinstance(value, Mapping) else None
    if not isinstance(value, Mapping):
        raise ValueError("Configure yandex.disk.documents for cleanup planning")
    paths = {}
    for key in ("source_path", "filtered_out_path"):
        path = value.get(key)
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"Configure yandex.disk.documents.{key}")
        paths[key] = path.strip()
    return {"source_root_path": paths["source_path"], "filtered_out_path": paths["filtered_out_path"]}


def execute(context: RunContext) -> dict[str, Any]:
    if context.options.limit is not None or context.options.workers != 1:
        raise ValueError("Cleanup requires the complete ISBN cohort: use one worker and no limit")
    if context.should_stop():
        return {"kind": "library.document_cleanup_preparation_summary", "outcome": "stopped"}
    paths = cleanup_paths()
    settings = load_settings()
    repository = DocumentCleanupRepository(settings.database_url, schema=settings.database_schema)
    try:
        context.log(f"document cleanup preparation start run_id={context.run_id}")
        summary = prepare_document_cleanup(
            repository=repository, **paths, should_stop=context.should_stop, log=context.log,
            on_progress=lambda current, total, counters: context.progress({
                "phase": "planning", "current": current, "total": total, **counters,
                "percent": round(current / total * 100, 2) if total else 100,
                "reviews_created": counters.get("isbn_reviews_created", 0),
            }),
        )
        return {**summary, "outcome": "stopped" if summary["stopped"] else "completed"}
    finally:
        repository.dispose()


def main() -> None:
    from app.cli import main as cli_main
    cli_main(["--task", TASK_ID])


if __name__ == "__main__":
    main()
