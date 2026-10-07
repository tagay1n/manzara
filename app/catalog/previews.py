"""Durable preview intent, leases, and immutable successful generations."""

from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from app.catalog.contracts import CatalogConflict, integer, nonblank


class PreviewStore:
    def request_preview(self, md5, *, actor, idempotency_key, recipe="webp-v2"):
        with self.engine.begin() as conn:
            doc = self._record(conn, "document", md5)
            if doc["mime_type"] != "application/pdf":
                raise ValueError("preview generation currently supports PDF documents")
            table = self.table("preview_requests")
            statement = insert(table).values(md5=md5, actor=nonblank(actor, "actor"),
                idempotency_key=nonblank(idempotency_key, "idempotency_key"), recipe=nonblank(recipe, "recipe"), private=doc["restricted"])
            conn.execute(statement.on_conflict_do_nothing(index_elements=[table.c.md5, table.c.idempotency_key]))
            return dict(conn.execute(select(table).where(table.c.md5 == md5, table.c.idempotency_key == idempotency_key)).mappings().one())

    def claim_preview(self, worker, *, lease_seconds=300):
        integer(lease_seconds, "lease_seconds")
        if lease_seconds > 3600:
            raise ValueError("preview lease must not exceed one hour")
        table = self.table("preview_requests")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            # One active generation per document. Expired claims are retryable.
            conn.exec_driver_sql("SELECT pg_advisory_xact_lock(hashtext('catalog-preview-claim'))")
            active = select(table.c.md5).where(table.c.status == "processing", table.c.lease_until > now)
            row = conn.execute(select(table).where(
                or_(table.c.status == "pending", (table.c.status == "processing") & (table.c.lease_until <= now)),
                ~table.c.md5.in_(active),
            ).order_by(table.c.request_id).limit(1).with_for_update(skip_locked=True)).mappings().first()
            if row is None:
                return None
            doc = self._record(conn, "document", row["md5"])
            return dict(conn.execute(table.update().where(table.c.request_id == row["request_id"]).values(
                status="processing", claim_token=uuid.uuid4().hex,
                lease_until=now + timedelta(seconds=lease_seconds), private=doc["restricted"],
            ).returning(table)).mappings().one())

    def finish_preview(self, request_id, claim_token, *, pages, source_page_count, actor, error=None):
        integer(request_id, "request_id")
        integer(source_page_count, "source_page_count", minimum=0)
        if not isinstance(pages, list):
            raise ValueError("preview pages must be an array")
        roles, page_numbers = set(), set()
        for page in pages:
            if not isinstance(page, dict) or set(page) - {"role", "page_number", "small_key", "large_key"}:
                raise ValueError("unsupported preview page")
            if page.get("role") not in {"first", "second", "last"} or page["role"] in roles:
                raise ValueError("preview roles must be distinct")
            number = integer(page.get("page_number"), "page_number")
            if number > source_page_count or number in page_numbers:
                raise ValueError("preview pages must be distinct and within the document")
            roles.add(page["role"])
            page_numbers.add(number)
            if not error:
                nonblank(page.get("small_key"), "small_key")
                nonblank(page.get("large_key"), "large_key")
        table, page_table = self.table("preview_requests"), self.table("preview_pages")
        with self.engine.begin() as conn:
            row = conn.execute(select(table).where(table.c.request_id == request_id).with_for_update()).mappings().one()
            if row["status"] != "processing" or row["claim_token"] != claim_token or row["lease_until"] <= datetime.now(timezone.utc):
                raise CatalogConflict("preview claim expired or was replaced")
            doc = self._record(conn, "document", row["md5"])
            if doc["restricted"] and not row["private"]:
                raise CatalogConflict("document access changed; regenerate into private storage")
            if not error:
                for page in pages:
                    conn.execute(page_table.insert().values(request_id=request_id, **page))
            after = dict(conn.execute(table.update().where(table.c.request_id == request_id).values(
                status="failed" if error else "ready", error=error, lease_until=None,
                source_page_count=source_page_count,
            ).returning(table)).mappings().one())
            self._audit(conn, "preview", request_id, dict(row), after, actor)
            return after

    def renew_preview(self, request_id, claim_token, *, lease_seconds=300):
        integer(request_id, "request_id")
        integer(lease_seconds, "lease_seconds")
        if lease_seconds > 3600:
            raise ValueError("preview lease must not exceed one hour")
        table = self.table("preview_requests")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            row = conn.execute(table.update().where(table.c.request_id == request_id,
                table.c.status == "processing", table.c.claim_token == claim_token, table.c.lease_until > now,
            ).values(lease_until=now + timedelta(seconds=lease_seconds)).returning(table)).mappings().first()
            if row is None:
                raise CatalogConflict("preview claim expired or was replaced")
            return dict(row)

    def preview(self, md5):
        table, pages = self.table("preview_requests"), self.table("preview_pages")
        with self.engine.begin() as conn:
            doc = self._record(conn, "document", md5)
            row = conn.execute(select(table).where(table.c.md5 == md5, table.c.status == "ready").order_by(table.c.request_id.desc()).limit(1)).mappings().first()
            if row is None:
                return None
            if doc["restricted"] and not row["private"]:
                return None
            result = dict(row)
            result["private"] = bool(row["private"] or doc["restricted"])
            result["pages"] = [dict(item) for item in conn.execute(select(pages).where(pages.c.request_id == row["request_id"]).order_by(pages.c.page_number)).mappings()]
            # Object locators are internal. HTTP assembly must provide authenticated
            # delivery for private previews rather than exposing a bucket URL.
            return result
