"""Persisted upstream source metadata."""

from sqlalchemy import Column, JSON, String

from .base import Base


class LibraryUpstreamMetadata(Base):
    """One source-page metadata payload keyed by document MD5."""

    __tablename__ = "library_upstream_metadata"

    md5 = Column(String, primary_key=True)
    payload_json = Column(JSON, nullable=False)


__all__ = ["LibraryUpstreamMetadata"]
