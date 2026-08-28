"""Persisted schema.org validation and re-extraction state."""

from sqlalchemy import BigInteger, Column, DateTime, JSON, String

from .base import Base


class LibraryMetadataQualityState(Base):
    """One versioned quality decision for a metadata row."""

    __tablename__ = "library_metadata_quality_state"

    md5 = Column(String, primary_key=True)
    contract_version = Column(String, nullable=False)
    status = Column(String, nullable=False, default="invalid")
    issues_json = Column(JSON, nullable=False, default=list)
    last_run_id = Column(BigInteger)
    detected_at = Column(DateTime(timezone=True))
    resolved_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True))


__all__ = ["LibraryMetadataQualityState"]
