"""Explicit catalog cleanup review commands replacing retired HTTP controls."""

import json
import re

from app.repositories.document_cleanup import DocumentCleanupRepository
from app.modules.library.document_cleanup_service import apply_isbn_review_decision
from app.modules.library.runtime.run_prepare_document_cleanup import cleanup_paths
from app.settings import load_settings
from app.task_runtime.session import SessionLock


def execute(arguments) -> None:
    settings = load_settings()
    session = SessionLock(settings.local_state_path)
    session.acquire()
    repository = None
    try:
        repository = DocumentCleanupRepository(settings.database_url, schema=settings.database_schema)
        command = arguments.cleanup_command
        if command == "reviews":
            result = repository.list_reviews(status=arguments.status, limit=arguments.limit)
        elif command == "queue":
            result = repository.list_queue(limit=arguments.limit)
        elif command == "decide":
            keep = [value.strip().lower() for value in arguments.keep]
            if any(not re.fullmatch(r"[0-9a-f]{32}", value) for value in keep):
                raise ValueError("--keep requires complete 32-character MD5 values")
            result = apply_isbn_review_decision(
                repository=repository, review_id=arguments.review_id, keep_md5s=keep, expected_snapshot=arguments.snapshot, **cleanup_paths(),
            )
        elif command == "undo":
            result = repository.undo_review(arguments.review_id)
        else:
            raise ValueError("Unsupported cleanup command")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        if repository is not None:
            repository.dispose()
        session.close()
