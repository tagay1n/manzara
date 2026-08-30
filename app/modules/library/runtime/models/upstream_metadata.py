"""Persisted upstream source metadata."""

from sqlalchemy import BigInteger, Column, DateTime, JSON, String

from .base import Base


class LibraryUpstreamMetadata(Base):
    """One source-page metadata payload keyed by document MD5."""

    __tablename__ = "library_upstream_metadata"

    md5 = Column(String, primary_key=True)
    payload_json = Column(JSON, nullable=False)
    source_key = Column(String, nullable=False, unique=True)
    source_etag = Column(String, nullable=False)
    source_size = Column(BigInteger, nullable=False)
    source_last_modified = Column(DateTime(timezone=True))
    payload_sha256 = Column(String, nullable=False)
    imported_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True))


__all__ = ["LibraryUpstreamMetadata"]
