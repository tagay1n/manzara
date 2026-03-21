"""Metadata ORM model."""

from sqlalchemy import Boolean, Column, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import relationship

from .base import Base


class Metadata(Base):
    """One-to-one schema.org metadata and library applicability for a document."""

    __tablename__ = "metadata"

    md5 = Column(String, ForeignKey("document.md5", ondelete="CASCADE"), primary_key=True)
    schema_org = Column(JSON)
    lib = Column(Boolean)
    lib_eval_method = Column(String)
    classification_id = Column(Integer, ForeignKey("classification.id", ondelete="SET NULL"), nullable=True)

    document = relationship("Document", back_populates="metadata_row")
    classification = relationship("Classification", back_populates="metadata_rows")

    def __str__(self):
        return "%s(%s)" % (
            type(self).__name__,
            ", ".join("%s=%s" % item for item in vars(self).items()),
        )

    def __repr__(self):
        return self.__str__()


__all__ = ["Metadata"]
