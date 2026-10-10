"""Metadata evaluation request and response records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool

from app.modules.library.runtime.metadata.schema import MetadataPatch


class Evaluation(BaseModel):
    """Validated publication inclusion/classification decision."""

    model_config = ConfigDict(extra="forbid")

    applicable: StrictBool
    reason: str | None = None
    metadata_patch: MetadataPatch | None = None
    library_ddc: str | None = None
    library_path: list[str] | None = None


@dataclass(frozen=True)
class EvaluationTask:
    """One publication and its observed metadata at the request boundary."""

    md5: str
    publication_id: int
    schema_org: dict[str, Any]
