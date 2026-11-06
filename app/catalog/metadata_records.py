"""Dictionary contracts crossing metadata discovery, processing, and guarded saves.

SQL envelopes retain their wire shape. Open catalog rows and Schema.org payloads
remain dictionaries; these records describe the orchestration-owned fields.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

JSONRecord = dict[str, Any]


class MetadataCredit(TypedDict):
    role: str
    role_name: str | None
    position: int
    nested_position: int
    raw_name: str
    kind: str
    contribution_id: NotRequired[int]
    resolution: NotRequired[str]
    display_name: NotRequired[str | None]


class NormalizedMetadata(TypedDict):
    publication_id: NotRequired[int]
    revision: NotRequired[int]
    scalars: JSONRecord
    languages: list[str]
    credits: list[MetadataCredit]
    identifiers: list[str]
    genres: list[str]
    subjects: list[JSONRecord]
    audiences: list[JSONRecord]
    audience_array: bool
    access_modes: list[str]
    sufficient_modes: list[list[str]]
    based_on: JSONRecord | None


class ClassificationPath(TypedDict):
    ddc: str
    path_en: list[str]


class PublicationSnapshot(TypedDict):
    publication_id: int
    revision: int
    metadata_present: bool
    metadata_review_order: int
    work_type: str | None
    name: str | None
    description: str | None
    edition: str | None
    date_published: str | None
    page_count: int | None


class MetadataSaveSnapshot(TypedDict):
    publication: PublicationSnapshot
    source: DiscoveredMetadataSource


class DiscoveredMetadataSource(TypedDict):
    md5: str
    publication_id: int
    revision: int
    mime_type: str
    selected: bool
    primary_revision: int
    source_revision: int | None
    content_revision: int | None
    document_url: str
    primary_storage_size: int
    content_url: str | None
    source_path: str | None
    upstream_metadata: JSONRecord | None
    inclusion: str
    evaluation_method: str | None
    publication_record: PublicationSnapshot
    metadata_record: NormalizedMetadata
    classification_path: ClassificationPath | None
    publication_protections: list[str]
    document_protections: list[str]


class MetadataSource(DiscoveredMetadataSource):
    """Prepared evidence with a frozen discovery snapshot and checkpoint identity."""

    save_snapshot: MetadataSaveSnapshot
    schema_org: JSONRecord
    checkpoint_id: str


class MetadataPublication(TypedDict):
    publication_id: int
    revision: int
    metadata_present: bool
    metadata_review_order: int
    name: str | None
    sources: list[MetadataSource]
    schema_org: JSONRecord
    evidence_md5: NotRequired[str]
