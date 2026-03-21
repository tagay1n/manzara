"""Rule helpers for sync filtering."""

from __future__ import annotations

import isbnlib

from .constants import NOT_DOCUMENT_TYPES


def normalize_isbn(value):
    """Normalize ISBN into compact 10/13-char form, otherwise return None."""
    cleaned = isbnlib.canonical(str(value).strip())
    if not cleaned:
        return None
    if isbnlib.is_isbn10(cleaned) or isbnlib.is_isbn13(cleaned):
        return cleaned
    return None


def should_be_skipped(file):
    """Determine whether a file should be skipped based on MIME/path rules."""
    if file.mime_type in NOT_DOCUMENT_TYPES:
        # sometimes valid PDF docs detected as octet-stream
        if file.mime_type == 'application/octet-stream' and file.path.endswith(".pdf"):
            return False, 'application/pdf'
        elif file.mime_type == 'text/html' and file.path.endswith(".txt"):
            return False, 'text/plain'
        elif file.mime_type == 'text/html' and file.path.endswith(".doc"):
            return False, 'text/plain'
        else:
            return True, file.mime_type
    return False, file.mime_type
