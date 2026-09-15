"""Metadata evaluation request and response records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.modules.library.runtime.metadata.schema import BookPatch


class Evaluation(BaseModel):
    """Classification result used to populate boolean `metadata.lib`."""

    applicable: bool = True
    reason: str | None = None
    metadata_patch: BookPatch | None = None
    library_ddc: str | None = None
    library_path: list[str] | None = None

    @classmethod
    def nonapplicable(cls, reason: str) -> "Evaluation":
        return cls(applicable=False, reason=reason)


@dataclass
class EvaluationTask:
    """Document payload needed for library applicability evaluation."""

    md5: str
    ya_path: str | None
    language: str | None
    page_count: int | None
    full: bool | None
    sharing_restricted: bool | None
    ya_public_url: str | None
    mime_type: str | None
    document_url: str | None
    upstream_metadata: dict[str, Any] | None
    content_url: str | None
    schema_org: dict | str | None
