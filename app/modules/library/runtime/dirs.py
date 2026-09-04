"""Centralized directory names used for local workspace storage."""

from enum import Enum


class Dirs(Enum):
    """Named subdirectories under the project workdir (~/.manzara)."""
    ENTRY_POINT = "cache/source-documents"
    CONTENT = "cache/extracted-document-content"
    METADATA = "durable/library/metadata"
    DOC_SLICES = "cache/metadata-evaluation-pdf-slices"
    PAGE_IMAGES = "workspaces/maintenance/metadata-evaluation/page-images"
    CLIPS = "workspaces/maintenance/metadata-evaluation/clips"
    CHUNKED_RESULTS = "workspaces/maintenance/metadata-evaluation/chunked-results"
    WIPING_PLAN = "durable/maintenance/cleanup-plans"
    PROMPTS = "workspaces/maintenance/metadata-evaluation/prompts"
    LOGS = "logs/legacy-metadata-evaluation"
    BOXES_PLOTS = "workspaces/maintenance/metadata-evaluation/plots"
    PREDICTIONS = "workspaces/maintenance/metadata-evaluation/predictions"
    PARQUET = "workspaces/maintenance/metadata-evaluation/parquet"
