"""Evaluate publication metadata through the interactive operations CLI."""

from app.task_runtime.contracts import RunContext


def execute(context: RunContext) -> dict:
    from app.modules.library.runtime.run_metadata_processing import execute as process
    return process(context, mode='evaluate')


def main() -> None:
    import sys
    from app.cli import main as cli_main
    cli_main(['--task', 'maintenance.monocorpus_meta_evaluate', *sys.argv[1:]])


if __name__ == '__main__':
    main()
