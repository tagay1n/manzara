"""CLI composition boundary: flow registrations stay outside shared runtime."""

from __future__ import annotations

import argparse
from functools import partial
from importlib import import_module
import re
import sys


def build_descriptors():
    from app.modules.library.collection_tasks import collection_task_definitions
    from app.modules.library.tasks import (
        LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID,
        library_task_definitions,
    )
    from app.modules.maintenance.tasks import (
        MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID,
        MAINTENANCE_MONOCORPUS_SYNC_TASK_ID,
        maintenance_task_definitions,
    )
    from app.task_runtime.contracts import TaskDescriptor

    handlers = {
        "library.normalize_personalities": "run_normalize_personalities",
        "library.extract_non_pdf": "run_extract_non_pdf",
        "library.generate_book_previews": "run_generate_book_previews",
        "library.site_export": "run_site_export",
        "library.suggest_publisher_merges": "run_suggest_publisher_merges",
        "library.collection_detect": "run_collection_detect",
        "library.metadata_extract": "run_metadata_extract",
        "maintenance.monocorpus_meta_evaluate": "run_meta_evaluate",
    }
    scheduled = {LIBRARY_PREPARE_DOCUMENT_CLEANUP_TASK_ID, MAINTENANCE_MONOCORPUS_SYNC_TASK_ID,
                 MAINTENANCE_DOCUMENT_S3_SYNC_TASK_ID}
    definitions = [*library_task_definitions(), *collection_task_definitions(),
                   *maintenance_task_definitions()]
    return [TaskDescriptor(
        task_id=item["task_id"], title=item["title"], group_id=item["group_id"],
        requires_full_inventory=item.get("requires_full_inventory", False),
        group="Maintenance" if item["task_id"].startswith("maintenance.") else "Library",
        execute=partial(_execute, handlers[item["task_id"]]),
    ) for item in definitions if item["task_id"] not in scheduled]


def _execute(module_name, context):
    return import_module(f"app.modules.library.runtime.{module_name}").execute(context)


def _positive(value: str) -> int:
    if not value.isascii() or not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(value)


def _md5(value: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{32}", value) is None:
        raise argparse.ArgumentTypeError("must be 32 hexadecimal digits")
    return value.lower()


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
    parser.add_argument("--limit", type=_positive, help="Optional candidate limit; metadata tasks count publications")
    parser.add_argument("--per-mime-limit", type=_positive, help="Non-PDF extraction: deterministic cohort cap per MIME")
    parser.add_argument("--only-md5", action="append", type=_md5, default=[], metavar="MD5",
                        help="Extraction/previews: restrict to these sources; repeat for a cohort")
    parser.add_argument("--retry-known-failures", action="store_true",
                        help="Extraction/previews: explicitly retry known failures; extraction also retries deferred results")
    args = parser.parse_args(arguments)
    if args.per_mime_limit is not None and (
        args.command is not None or args.task != "library.extract_non_pdf"
    ):
        parser.error("--per-mime-limit requires --task library.extract_non_pdf")
    if (args.only_md5 or args.retry_known_failures) and (
        args.command is not None or args.task not in {"library.extract_non_pdf", "library.generate_book_previews"}
    ):
        parser.error("Source cohort/retry options require --task library.extract_non_pdf or library.generate_book_previews")
    if args.command is None:
        descriptor = next((item for item in build_descriptors() if item.task_id == args.task), None)
        if descriptor is None:
            parser.error(f"Unknown task ID: {args.task}")
        if descriptor.requires_full_inventory and args.limit is not None:
            parser.error(f"{descriptor.title} requires the complete inventory; omit --limit")
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
        from app.artifacts import cache_dir
        from app.modules.library.preview_detection import configure_detector_environment
        from app.cli.terminal import Terminal
        configure_detector_environment(cache_dir("downloaded-models", "huggingface"))
        exit_code = asyncio.run(Terminal(args, build_descriptors).run())
        if exit_code:
            parser.exit(exit_code)
    except ImportError as exc:
        parser.exit(2, f"CLI dependency unavailable ({exc.name}); install requirements.txt.\n")
    except KeyboardInterrupt:
        parser.exit(130)
