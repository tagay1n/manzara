"""Persistence model for ISBN groups explicitly allowed to keep many docs."""

from sqlalchemy import Column, DateTime, String, func

from .base import Base


class IsbnKeepMany(Base):
    """Stores md5 members for ISBN groups where multiple docs are intentionally kept."""

    __tablename__ = "isbn_keep_many"

    isbn_key = Column(String, primary_key=True)
    md5 = Column(String, primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __str__(self):
        return "%s(%s)" % (
            type(self).__name__,
            ", ".join("%s=%s" % item for item in vars(self).items()),
        )

    def __repr__(self):
        return self.__str__()


__all__ = ["IsbnKeepMany"]
