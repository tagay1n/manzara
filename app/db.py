"""Stable public database facade composed from focused repositories."""

from app.repositories.core import CoreRepository
from app.repositories.gemini import GeminiRepository
from app.repositories.normalization import NormalizationRepository
from app.repositories.publisher_merges import PublisherMergeRepository
from app.repositories.runs import RunRepository


class Database(
    RunRepository,
    GeminiRepository,
    NormalizationRepository,
    PublisherMergeRepository,
    CoreRepository,
):
    """Facade over durable PostgreSQL and disposable local SQLite state."""


__all__ = [
    "Database",
]
