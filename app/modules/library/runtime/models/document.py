"""Document ORM model."""

from sqlalchemy import BigInteger, Boolean, Column, DateTime, String
from sqlalchemy.orm import relationship

from .base import Base


class Document(Base):
    """Represents a document record with storage pointers and extraction state."""

    __tablename__ = "document"

    md5 = Column(String, primary_key=True, nullable=False, unique=True, index=True)
    mime_type = Column(String)
    ya_path = Column(String)
    ya_public_url = Column(String)
    ya_public_key = Column(String)
    ya_resource_id = Column(String)
    language = Column(String)
    content_extraction_method = Column(String)
    meta_extraction_method = Column(String)
    full = Column(Boolean)
    sharing_restricted = Column(Boolean)
    document_url = Column(String)
    content_url = Column(String)
    primary_storage_size = Column(BigInteger)
    primary_storage_etag = Column(String)
    primary_storage_verified_at = Column(DateTime(timezone=True))
    metadata_row = relationship(
        "Metadata",
        uselist=False,
        back_populates="document",
        lazy="joined",
        cascade="all, delete-orphan",
        single_parent=True,
    )

    def __str__(self):
        return "%s(%s)" % (
            type(self).__name__,
            ", ".join("%s=%s" % item for item in vars(self).items()),
        )

    def __repr__(self):
        return self.__str__()


__all__ = ["Document"]
