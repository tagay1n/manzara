"""PostgreSQL queue and checkpoints for legacy PDF content migration."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.document_storage import object_url, parse_object_url
from app.modules.maintenance.content_storage_migration import ContentMigrationCandidate
from app.postgres_engine import acquire_postgres_engine, release_postgres_engine

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ContentStorageMigrationRepository:
    def __init__(
        self,
        database_url: str,
        *,
        schema: str,
        legacy_endpoint: str,
        legacy_bucket: str,
    ) -> None:
        if not _SCHEMA_RE.fullmatch(schema):
            raise ValueError(f"Invalid database schema: {schema!r}")
        self.engine: Engine = acquire_postgres_engine(database_url, schema=schema)
        self.legacy_endpoint = legacy_endpoint
        self.legacy_bucket = legacy_bucket
        self.legacy_prefix = object_url(legacy_endpoint, legacy_bucket, "")

    def dispose(self) -> None:
        release_postgres_engine(self.engine)

    def list_work(
        self, *, md5: str | None = None, limit: int | None = None
    ) -> list[ContentMigrationCandidate]:
        sql = """
            SELECT d.md5, d.mime_type,
                   COALESCE(state.source_content_url, d.content_url) source_content_url,
                   COALESCE(state.status, 'pending') status
            FROM document d
            LEFT JOIN maintenance_content_migration state ON state.md5 = d.md5
            WHERE LOWER(BTRIM(COALESCE(d.mime_type, ''))) = 'application/pdf'
              AND (
                    state.status IN ('copying', 'cutover', 'deleting', 'failed')
                    OR (
                        state.md5 IS NULL
                        AND d.content_url LIKE :legacy_prefix
                    )
                  )
              AND (:md5 IS NULL OR d.md5 = :md5)
            ORDER BY d.md5
        """
        params: dict[str, Any] = {
            "legacy_prefix": self.legacy_prefix + "%",
            "md5": str(md5).strip().lower() if md5 else None,
        }
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = int(limit)
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        result: list[ContentMigrationCandidate] = []
        for row in rows:
            digest = str(row.get("md5") or "").strip().lower()
            source_url = str(row.get("source_content_url") or "").strip()
            parsed = parse_object_url(source_url, self.legacy_endpoint)
            if parsed != (self.legacy_bucket, f"{digest}.zip"):
                raise RuntimeError(
                    f"PDF {digest} has unexpected legacy content URL: {source_url}"
                )
            result.append(
                ContentMigrationCandidate(
                    digest,
                    str(row.get("mime_type") or ""),
                    source_url,
                    str(row.get("status") or "pending"),
                )
            )
        return result

    def count_pending(self) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM document d
                        LEFT JOIN maintenance_content_migration state
                          ON state.md5 = d.md5
                        WHERE LOWER(BTRIM(COALESCE(d.mime_type, '')))
                                  = 'application/pdf'
                          AND (
                                state.status IN (
                                    'copying', 'cutover', 'deleting', 'failed'
                                )
                                OR (
                                    state.md5 IS NULL
                                    AND d.content_url LIKE :legacy_prefix
                                )
                              )
                        """
                    ),
                    {"legacy_prefix": self.legacy_prefix + "%"},
                ).scalar_one()
            )

    def start(self, candidate: ContentMigrationCandidate, *, run_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO maintenance_content_migration (
                        md5, source_content_url, source_mime_type, status,
                        attempt_count, last_run_id, created_at, updated_at
                    ) VALUES (
                        :md5, :source_url, :mime_type, 'copying', 1, :run_id,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (md5) DO UPDATE SET
                        status = CASE
                            WHEN maintenance_content_migration.status
                                 IN ('cutover', 'deleting')
                            THEN maintenance_content_migration.status
                            ELSE 'copying'
                        END,
                        attempt_count =
                            maintenance_content_migration.attempt_count + 1,
                        last_run_id = EXCLUDED.last_run_id,
                        error_text = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "md5": candidate.md5,
                    "source_url": candidate.source_content_url,
                    "mime_type": candidate.mime_type,
                    "run_id": run_id,
                },
            )

    def checkpoint_image(self, md5: str, image_key: str, **values: Any) -> None:
        params = {"md5": md5, "image_key": image_key, **values}
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO maintenance_content_migration_images (
                        md5, image_key, source_etag, source_size,
                        destination_etag, destination_size, sha256,
                        source_deleted, last_run_id, created_at, updated_at
                    ) VALUES (
                        :md5, :image_key, :source_etag, :source_size,
                        :destination_etag, :destination_size, :sha256,
                        :source_deleted, :run_id, CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (md5, image_key) DO UPDATE SET
                        source_etag=EXCLUDED.source_etag,
                        source_size=EXCLUDED.source_size,
                        destination_etag=EXCLUDED.destination_etag,
                        destination_size=EXCLUDED.destination_size,
                        sha256=EXCLUDED.sha256,
                        source_deleted=EXCLUDED.source_deleted,
                        last_run_id=EXCLUDED.last_run_id,
                        updated_at=CURRENT_TIMESTAMP
                    """
                ),
                params,
            )

    def retain_images(self, md5: str, image_keys: tuple[str, ...]) -> None:
        expected = set(image_keys)
        with self.engine.begin() as conn:
            existing = conn.execute(
                text(
                    "SELECT image_key FROM maintenance_content_migration_images "
                    "WHERE md5=:md5"
                ),
                {"md5": md5},
            ).scalars()
            for image_key in existing:
                if str(image_key) not in expected:
                    conn.execute(
                        text(
                            "DELETE FROM maintenance_content_migration_images "
                            "WHERE md5=:md5 AND image_key=:image_key"
                        ),
                        {"md5": md5, "image_key": str(image_key)},
                    )

    def list_images(self, md5: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT image_key, source_etag, source_size,
                               destination_etag, destination_size, sha256,
                               source_deleted
                        FROM maintenance_content_migration_images
                        WHERE md5=:md5
                        ORDER BY image_key
                        """
                    ),
                    {"md5": md5},
                ).mappings()
            ]

    def get_archive_checkpoint(self, md5: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT source_archive_etag, source_archive_size,
                           destination_content_url, destination_archive_etag,
                           destination_archive_size, destination_archive_sha256,
                           markdown_member, source_archive_deleted
                    FROM maintenance_content_migration
                    WHERE md5=:md5
                    """
                ),
                {"md5": md5},
            ).mappings().first()
        return dict(row) if row is not None else None

    def checkpoint_archive(self, md5: str, **values: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE maintenance_content_migration SET
                        source_archive_etag=:source_etag,
                        source_archive_size=:source_size,
                        destination_content_url=:destination_url,
                        destination_archive_etag=:destination_etag,
                        destination_archive_size=:destination_size,
                        destination_archive_sha256=:sha256,
                        markdown_member=:markdown_member,
                        last_run_id=:run_id,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE md5=:md5
                    """
                ),
                {"md5": md5, **values},
            )

    def cutover(
        self,
        md5: str,
        *,
        expected_url: str,
        destination_url: str,
        expected_mime_type: str,
        run_id: int,
    ) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE document
                    SET content_url=:destination_url
                    WHERE md5=:md5
                      AND content_url IS NOT DISTINCT FROM :expected_url
                      AND mime_type IS NOT DISTINCT FROM :expected_mime_type
                    """
                ),
                {
                    "md5": md5,
                    "expected_url": expected_url,
                    "destination_url": destination_url,
                    "expected_mime_type": expected_mime_type,
                },
            )
            if int(result.rowcount or 0) != 1:
                return False
            conn.execute(
                text(
                    """
                    UPDATE maintenance_content_migration
                    SET status='cutover', last_run_id=:run_id,
                        error_text=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE md5=:md5
                    """
                ),
                {"md5": md5, "run_id": run_id},
            )
        return True

    def checkpoint_archive_deleted(self, md5: str, *, run_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE maintenance_content_migration
                    SET source_archive_deleted=TRUE, status='deleting',
                        last_run_id=:run_id, updated_at=CURRENT_TIMESTAMP
                    WHERE md5=:md5
                    """
                ),
                {"md5": md5, "run_id": run_id},
            )

    def checkpoint_image_deleted(self, md5: str, image_key: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE maintenance_content_migration_images
                    SET source_deleted=TRUE, updated_at=CURRENT_TIMESTAMP
                    WHERE md5=:md5 AND image_key=:image_key
                    """
                ),
                {"md5": md5, "image_key": image_key},
            )

    def complete(self, md5: str, *, run_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE maintenance_content_migration
                    SET status='completed', completed_at=CURRENT_TIMESTAMP,
                        last_run_id=:run_id, error_text=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE md5=:md5
                    """
                ),
                {"md5": md5, "run_id": run_id},
            )

    def fail(self, md5: str, *, run_id: int, error_text: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE maintenance_content_migration
                    SET status=CASE WHEN status IN ('cutover', 'deleting')
                                    THEN 'deleting' ELSE 'failed' END,
                        last_run_id=:run_id, error_text=:error_text,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE md5=:md5
                    """
                ),
                {"md5": md5, "run_id": run_id, "error_text": error_text[:4000]},
            )


__all__ = ["ContentStorageMigrationRepository"]
