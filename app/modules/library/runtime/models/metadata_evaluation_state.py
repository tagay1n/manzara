"""Persistent per-model metadata evaluation attempts."""

from sqlalchemy import BigInteger, Column, DateTime, JSON, String, Text

from .base import Base


class LibraryMetadataEvaluationState(Base):
    """Resume one document with the next untried evaluation model."""

    __tablename__ = "library_metadata_evaluation_state"

    md5 = Column(String, primary_key=True)
    status = Column(String, nullable=False, default="partial")
    attempts_json = Column(JSON, nullable=False, default=list)
    model_pool_json = Column(JSON, nullable=False, default=list)
    last_run_id = Column(BigInteger)
    terminal_reason = Column(Text)
    prompt_version = Column(String)
    created_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True))


__all__ = ["LibraryMetadataEvaluationState"]
