"""Cross-process, per-document coordination without long SQL transactions."""

from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import text

from app.catalog.contracts import CatalogConflict, document_md5


class DocumentOperationBusy(CatalogConflict):
    """Another operation owns this document; retry on a later run."""


_OWNER = ContextVar("document_operation_owner", default=None)


def _parameters(md5):
    return {"identity": f"catalog-document-storage:{document_md5(md5)}"}


def lock_document_transaction(conn, md5):
    """Try before row/review locks; never wait behind a remote operation."""
    parameters = _parameters(md5)
    owner = _OWNER.get()
    if owner and owner[0] is conn.engine and owner[1] == md5:
        if owner[2].closed or owner[2].invalidated:
            raise CatalogConflict("Document operation lost its database session")
        return
    locked = conn.execute(text("SELECT pg_try_advisory_xact_lock("
                               "hashtext(current_schema()), hashtext(:identity))"), parameters).scalar_one()
    if not locked:
        raise DocumentOperationBusy(f"Document {md5} is busy; retry after the current operation")


def check_document_operation(conn):
    """Do not reconnect an invalidated session and mistake it for ownership."""
    owner = _OWNER.get()
    if owner is None or owner[2] is not conn:
        raise CatalogConflict("Document operation requires its lock-owning connection")
    if conn.closed or conn.invalidated:
        raise CatalogConflict("Document operation lost its database session")
    if conn.in_transaction():
        raise RuntimeError("Remote document boundaries require a closed SQL transaction")
    try:
        conn.exec_driver_sql("SELECT 1")
        conn.rollback()
    except BaseException:
        conn.invalidate()
        raise


@contextmanager
def document_operation(engine, md5):
    """Reserve one pool connection; session locks survive short commits."""
    parameters = _parameters(md5)
    with engine.connect() as conn:
        try:
            locked = conn.execute(text("SELECT pg_try_advisory_lock("
                                       "hashtext(current_schema()), hashtext(:identity))"), parameters).scalar_one()
            conn.rollback()
        except BaseException:
            # Acquisition may have reached PostgreSQL even if its response failed.
            conn.invalidate()
            raise
        if not locked:
            raise DocumentOperationBusy(f"Document {md5} is busy; retry after the current operation")
        token = _OWNER.set((engine, md5, conn))
        try:
            yield conn
        finally:
            _OWNER.reset(token)
            try:
                if conn.closed or conn.invalidated:
                    raise CatalogConflict("Document operation lost its database session")
                conn.rollback()
                unlocked = conn.execute(text("SELECT pg_advisory_unlock("
                                             "hashtext(current_schema()), hashtext(:identity))"), parameters).scalar_one()
                conn.rollback()
                if not unlocked:
                    raise CatalogConflict("Document operation lock ownership was lost")
            except BaseException:
                conn.invalidate()
                raise
