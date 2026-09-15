"""Shared non-PDF extraction records, errors, and recipe version."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXTRACTOR_VERSION = "nonpdf.v7"


@dataclass(frozen=True)
class ExtractedAsset:
    source_ref: str
    path: Path
    ordinal: int


@dataclass(frozen=True)
class PreparedExtraction:
    detected_format: str
    workspace: Path
    ast: dict[str, Any] | None
    text: str | None
    assets: tuple[ExtractedAsset, ...]
    legacy_conversion: str | None = None


class UnsupportedDocumentFormat(ValueError):
    def __init__(self, detected_format: str) -> None:
        self.detected_format = str(detected_format or "unknown")
        super().__init__(f"Unsupported document format: {self.detected_format}")


class ConverterCommandError(RuntimeError):
    """A converter rejected one input document."""


class ConverterTimeoutError(RuntimeError):
    """A converter exceeded its operational time limit."""
