"""Ordered single-document catalog locks within a caller-owned transaction."""

from sqlalchemy import text

from app.document_operation_lock import lock_document_transaction


def lock_document_rows(conn, md5):
    """Guard storage operations, then lock the document and ordered locations."""
    lock_document_transaction(conn, md5)
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext(:identity))"),
                 {"identity": f"catalog-document:{md5}"})
    row = conn.execute(text("SELECT * FROM catalog_documents WHERE md5=:md5 FOR UPDATE"),
                       {"md5": md5}).mappings().one_or_none()
    conn.execute(text("SELECT location_id FROM catalog_locations WHERE md5=:md5 ORDER BY location_id FOR UPDATE"),
                 {"md5": md5}).all()
    return dict(row) if row else None
