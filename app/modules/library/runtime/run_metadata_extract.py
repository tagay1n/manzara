"""Extract publication metadata through the interactive operations CLI."""

from app.task_runtime.contracts import RunContext


def execute(context: RunContext) -> dict:
    from app.modules.library.runtime.run_metadata_processing import execute as process
    return process(context, mode='extract')
