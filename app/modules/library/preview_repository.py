"""Read successful previews from their sole durable catalog generation."""

import re
from sqlalchemy import text

from app.postgres_engine import acquire_postgres_engine, release_postgres_engine


class LibraryPreviewRepository:
    def __init__(self, database_url, *, schema="monocorpus"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("Invalid database schema")
        self.schema = schema
        self._engine = acquire_postgres_engine(database_url, schema=schema)

    def dispose(self):
        release_postgres_engine(self._engine)

    def get(self, md5):
        with self._engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM catalog_selected_previews WHERE md5=:md5"),
                               {"md5": md5}).mappings().first()
        return dict(row) if row else None

    def is_eligible_pdf(self, md5, *, endpoint_url, public_bucket):
        with self._engine.connect() as conn:
            return bool(conn.execute(text("""SELECT EXISTS(SELECT 1 FROM catalog_documents d
                JOIN catalog_publications p USING(publication_id)
                JOIN catalog_locations l ON l.md5=d.md5 AND l.provider='s3' AND l.purpose='primary'
                WHERE d.md5=:md5 AND p.inclusion='included' AND d.mime_type='application/pdf'
                AND NOT d.restricted AND l.locator LIKE :prefix)"""),
                {"md5": md5, "prefix": f"{endpoint_url.rstrip('/')}/{public_bucket}/%"}).scalar())

    def get_stats(self, *, recipe_version, endpoint_url, public_bucket):
        with self._engine.connect() as conn:
            row = conn.execute(text("""SELECT count(*) AS eligible,
                count(*) FILTER(WHERE v.recipe_version=:recipe) AS ready,
                coalesce(sum(num_nonnulls(v.first_preview_page,v.second_preview_page,v.last_preview_page))
                    FILTER(WHERE v.recipe_version=:recipe),0) AS pages
                FROM catalog_documents d JOIN catalog_publications p USING(publication_id)
                JOIN catalog_locations l ON l.md5=d.md5 AND l.provider='s3' AND l.purpose='primary'
                LEFT JOIN catalog_selected_previews v ON v.md5=d.md5
                WHERE p.inclusion='included' AND d.mime_type='application/pdf' AND NOT d.restricted
                AND l.locator LIKE :prefix"""),
                {"recipe": recipe_version, "prefix": f"{endpoint_url.rstrip('/')}/{public_bucket}/%"}).mappings().one()
        return {"recipe_version": recipe_version, "eligible": row["eligible"], "ready": row["ready"],
                "pending": row["eligible"] - row["ready"], "partial": 0, "failed": 0,
                "generated_preview_pages": row["pages"], "generated_image_objects": row["pages"] * 2}
