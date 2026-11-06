"""Dictionary contracts crossing metadata discovery, processing, and guarded saves.

SQL envelopes retain their wire shape. Open catalog rows and Schema.org payloads
remain dictionaries; these records describe the orchestration-owned fields.
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

JSONRecord = dict[str, Any]


class MetadataSaveSnapshot(TypedDict):
    publication: JSONRecord
    source: MetadataSource


class MetadataSource(TypedDict):
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
    publication_record: JSONRecord
    metadata_record: JSONRecord
    classification_path: JSONRecord | None
    publication_protections: list[str]
    document_protections: list[str]
    save_snapshot: NotRequired[MetadataSaveSnapshot]
    schema_org: NotRequired[JSONRecord]
    checkpoint_id: NotRequired[str]


class MetadataPublication(TypedDict):
    publication_id: int
    revision: int
    metadata_present: bool
    metadata_review_order: int
    name: str | None
    sources: list[MetadataSource]
    schema_org: NotRequired[JSONRecord]
    evidence_md5: NotRequired[str]
