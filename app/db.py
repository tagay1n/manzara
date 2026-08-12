"""Stable public database facade composed from focused repositories."""

from app.repositories.core import CoreRepository, utc_now
from app.repositories.definitions import DefinitionsRepository
from app.repositories.gemini import GeminiRepository
from app.repositories.normalization import NormalizationRepository
from app.repositories.runs import RunRepository
from app.repositories.shayan import ShayanRepository
from app.repositories.workflows import WorkflowRepository
from app.runtime_states import (
    TASK_RUN_ACTIVE_STATUSES as ACTIVE_STATUSES,
    WORKFLOW_RUN_ACTIVE_STATUSES as ACTIVE_WORKFLOW_STATUSES,
)


class Database(
    DefinitionsRepository,
    WorkflowRepository,
    RunRepository,
    GeminiRepository,
    ShayanRepository,
    NormalizationRepository,
    CoreRepository,
):
    """PostgreSQL facade preserving the historical ``app.db.Database`` API."""


__all__ = ["ACTIVE_STATUSES", "ACTIVE_WORKFLOW_STATUSES", "Database", "utc_now"]
