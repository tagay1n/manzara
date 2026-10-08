"""CLI composition boundary: flow registrations stay outside shared runtime."""

from __future__ import annotations

import argparse
import sys


def build_descriptors(settings):
    from app.modules.library.collection_tasks import collection_task_definitions
    from app.modules.library.tasks import library_task_definitions
    from app.modules.maintenance.tasks import maintenance_task_definitions
    from app.task_runtime.contracts import TaskDescriptor

    def normalize(context):
        from app.modules.library.runtime.run_normalize_personalities import execute
        return execute(context)

    def cleanup(context):
        from app.modules.library.runtime.run_prepare_document_cleanup import execute
        return execute(context)

    handlers = {"library.normalize_personalities": normalize,
                "library.prepare_document_cleanup": cleanup}
    definitions = [*library_task_definitions(), *collection_task_definitions(),
                   *maintenance_task_definitions(settings.maintenance)]
    return [TaskDescriptor(
        task_id=item["task_id"], title=item["title"], definition=item,
        group="Maintenance" if item["task_id"].startswith("maintenance.") else "Library",
        execute=handlers.get(item["task_id"]),
    ) for item in definitions]


def _positive(value: str) -> int:
    if not value.isascii() or not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(value)


def main(arguments: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="manzara", description="Manzara interactive task operations")
    commands = parser.add_subparsers(dest="command")
    cleanup = commands.add_parser("cleanup", help="Inspect cleanup plans and review duplicate ISBNs")
    cleanup_commands = cleanup.add_subparsers(dest="cleanup_command", required=True)
    reviews = cleanup_commands.add_parser("reviews", help="Print reviewed candidates as JSON")
    reviews.add_argument("--status", choices=("pending", "decided", "superseded"), default="pending")
    reviews.add_argument("--limit", type=_positive, default=100)
    queue = cleanup_commands.add_parser("queue", help="Print persisted cleanup plans as JSON")
    queue.add_argument("--limit", type=_positive, default=100)
    decide = cleanup_commands.add_parser("decide", help="Keep explicit MD5s and queue the other reviewed files")
    decide.add_argument("review_id", type=_positive)
    decide.add_argument("--snapshot", required=True, help="review_snapshot from cleanup reviews")
    decide.add_argument("--keep", nargs="+", required=True, metavar="MD5")
    undo = cleanup_commands.add_parser("undo", help="Undo a decision before cleanup starts")
    undo.add_argument("review_id", type=_positive)
    parser.add_argument("--task", default="library.normalize_personalities", help="Initially selected task ID")
    parser.add_argument("--workers", type=_positive, help="Worker count for normalization")
    parser.add_argument("--limit", type=_positive, help="Optional normalization candidate limit")
    args = parser.parse_args(arguments)
    if args.command == "cleanup":
        from app.modules.library.cleanup_cli import execute
        try:
            execute(args)
        except Exception as exc:
            from app.task_runtime.logging import redact
            parser.exit(2, f"Cleanup failed: {redact(exc)}\n")
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.exit(2, "Manzara execution requires an interactive terminal. Use --help for options.\n")
    try:
        import asyncio
        from app.cli.terminal import Terminal
        asyncio.run(Terminal(args, build_descriptors).run())
    except ImportError as exc:
        parser.exit(2, f"CLI dependency unavailable ({exc.name}); install requirements.txt.\n")
    except KeyboardInterrupt:
        parser.exit(130)
