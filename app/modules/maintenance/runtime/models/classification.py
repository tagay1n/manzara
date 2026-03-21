"""Classification ORM model for library taxonomy."""

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import relationship

from .base import Base


class Classification(Base):
    """Canonical classification entry with multilingual labels."""

    __tablename__ = "classification"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ddc = Column(String, nullable=False, index=True)
    path_en = Column(JSON, nullable=False)
    path_en_key = Column(String, nullable=False)
    path_tt = Column(JSON, nullable=True)
    status = Column(String, nullable=False, default="pending")
    created_by = Column(String, nullable=False, default="gemini")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    metadata_rows = relationship("Metadata", back_populates="classification")

    __table_args__ = (
        UniqueConstraint("ddc", "path_en_key", name="uq_classification_ddc_path_en_key"),
    )


__all__ = ["Classification"]

