"""Extract publication metadata through the interactive operations CLI."""

from app.task_runtime.contracts import RunContext


def execute(context: RunContext) -> dict:
    from app.modules.library.runtime.run_metadata_processing import execute as process
    return process(context, mode='extract')


def main() -> None:
    import sys
    from app.cli import main as cli_main
    cli_main(['--task', 'library.metadata_extract', *sys.argv[1:]])


if __name__ == '__main__':
    main()
