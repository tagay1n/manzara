"""Stable public database facade composed from focused repositories."""

from app.repositories.core import CoreRepository, utc_now
from app.repositories.conveyor import ConveyorRepository
from app.repositories.definitions import DefinitionsRepository
from app.repositories.gemini import GeminiRepository
from app.repositories.normalization import NormalizationRepository
from app.repositories.runs import RunRepository
from app.runtime_states import (
    TASK_RUN_ACTIVE_STATUSES as ACTIVE_STATUSES,
)


class Database(
    DefinitionsRepository,
    ConveyorRepository,
    RunRepository,
    GeminiRepository,
    NormalizationRepository,
    CoreRepository,
):
    """PostgreSQL facade preserving the historical ``app.db.Database`` API."""


__all__ = ["ACTIVE_STATUSES", "Database", "utc_now"]
