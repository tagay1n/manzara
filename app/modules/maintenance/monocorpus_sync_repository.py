"""PostgreSQL catalog and cleanup state for guarded monocorpus synchronization."""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Mapping

from sqlalchemy import text

from app.repositories.document_cleanup import DocumentCleanupRepository
from app.operational_state import configured_store
from app.catalog.document_sync import DocumentSyncStore
from app.catalog.document_sync_bulk import update_records
from app.catalog.contracts import CatalogConflict, document_md5, integer
from app.document_cleanup_contracts import (
    CLEANUP_ACTIONS_BY_SCOPE, CLEANUP_EXECUTION_PHASES,
    CLEANUP_PHASE_DATABASE, CLEANUP_PHASE_YANDEX,
)
from app.document_cleanup_paths import cleanup_source_path, source_path
from app.document_operation_lock import document_operation


class MonocorpusSyncRepository(DocumentCleanupRepository):
    """Extend cleanup persistence with catalog synchronization operations."""

    def __init__(self, database_url: str, *, schema: str) -> None:
        super().__init__(database_url, schema=schema)
        self.catalog = DocumentSyncStore(self.engine, schema=schema)

    @contextmanager
    def sync_lock(self):
        """One sync writer per schema, including clients with other local stores."""
        if self.engine.pool.size() < 3:
            raise RuntimeError('Sync requires MANZARA_DB_POOL_SIZE >= 3 for schema/document locks and short transactions')
        with self.engine.connect() as conn:
            locked = conn.execute(text("SELECT pg_try_advisory_lock(hashtext(current_schema()), hashtext('maintenance.monocorpus_sync'))")).scalar_one()
            conn.rollback()
            if not locked:
                raise RuntimeError("Another Sync run owns this catalog; wait for it to finish")
            try:
                yield
            finally:
                try:
                    conn.execute(text("SELECT pg_advisory_unlock(hashtext(current_schema()), hashtext('maintenance.monocorpus_sync'))"))
                    conn.rollback()
                except Exception:
                    conn.invalidate()
                    raise

    def list_documents(self) -> dict[str, dict[str, Any]]:
        return self.catalog.list_documents()

    @contextmanager
    def cleanup_operation(self, cleanup_id):
        with self.engine.connect() as conn:
            md5 = conn.execute(text('SELECT md5 FROM document_cleanup_queue WHERE cleanup_id=:id'),
                               {'id': cleanup_id}).scalar_one()
        with document_operation(self.engine, md5) as conn:
            yield conn

    def list_active_cleanup(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT cleanup_id, scope, action, reason, md5,
                               source_resource_id, source_path, target_path,
                               status, phase, evidence_json
                        FROM document_cleanup_queue
                        WHERE status IN ('planned', 'running', 'failed')
                        ORDER BY cleanup_id
                        """
                    )
                ).mappings()
            ]

    def claim_cleanup(self, cleanup_id: int, *, run_id: int) -> dict[str, Any] | None:
        integer(cleanup_id, "cleanup_id")
        with self.write_transaction() as conn:
            self.lock_isbn_reviews_in_transaction(conn)
            row = conn.execute(text("""SELECT * FROM document_cleanup_queue
                WHERE cleanup_id=:cleanup_id AND status IN ('planned','running','failed')
                FOR UPDATE"""), {"cleanup_id": cleanup_id}).mappings().one_or_none()
            if row is None:
                return None
            item = dict(row)
            document_md5(item['md5'])
            cleanup_source_path(item)
            if item['action'] not in CLEANUP_ACTIONS_BY_SCOPE.get(item['scope'], ()):
                raise CatalogConflict('Unsupported cleanup scope/action')
            evidence = dict(item['evidence_json'])
            if item['scope'] == 'document':
                self.catalog._lock(conn, item['md5'])
                current = self.catalog._snapshot(conn, item['md5'])
                expected = evidence.get('execution_document')
                if expected is None:
                    if current is None:
                        raise CatalogConflict("Cleanup document is missing before execution; inspect the plan")
                    if source_path(current['ya_path']) != source_path(item['source_path']):
                        raise CatalogConflict("Cleanup source path changed; inspect the plan")
                    planning = self.list_documents_for_planning(conn=conn, md5s=[item['md5']])[0]
                    for key in ('publication_id', 'document_revision', 'publication_revision', 'source_revision'):
                        if evidence.get(key) is not None and evidence[key] != planning[key]:
                            raise CatalogConflict(f"Cleanup reviewed {key} changed; refresh the plan/review")
                    evidence['execution_document'] = {key: current[key] for key in
                        ('publication_id', 'document_revision', 'source_revision')}
                elif current and any(current[key] != expected[key] for key in
                                     ('publication_id', 'document_revision', 'source_revision')):
                    raise CatalogConflict("Cleanup catalog facts changed after execution began; inspect the plan")
                elif current is None and item['phase'] != CLEANUP_PHASE_DATABASE:
                    raise CatalogConflict("Cleanup document disappeared before the database phase")
                if item['reason'] == 'duplicate_isbn':
                    review_id = integer(evidence.get('review_id'), 'review_id')
                    review = conn.execute(text("SELECT status,keep_md5s_json,candidates_json FROM library_isbn_duplicate_reviews WHERE review_id=:id"),
                                          {"id": review_id}).mappings().one_or_none()
                    if review is None or review['status'] != 'decided' or item['md5'] in review['keep_md5s_json']:
                        raise CatalogConflict("ISBN cleanup requires a current decided review excluding this file")
                    if item['md5'] not in {candidate['md5'] for candidate in review['candidates_json']}:
                        raise CatalogConflict("ISBN cleanup file is outside the decided review")
            phase = CLEANUP_PHASE_YANDEX if item['phase'] == 'planned' else item['phase']
            if phase not in CLEANUP_EXECUTION_PHASES:
                raise CatalogConflict(f"Unknown cleanup phase {phase}; inspect the plan")
            claimed = dict(conn.execute(text("""UPDATE document_cleanup_queue SET status='running',
                phase=:phase,evidence_json=CAST(:evidence AS JSONB),updated_at=CURRENT_TIMESTAMP
                WHERE cleanup_id=:id RETURNING *"""),
                {"id": cleanup_id, "phase": phase, "evidence": json.dumps(evidence, default=lambda value: value.isoformat())}).mappings().one())
        store = configured_store()
        with store.transaction() as local:
            previous = local.execute("SELECT payload_json FROM operational_items WHERE scope=? AND item_id=?",
                                     ("maintenance.cleanup", str(cleanup_id))).fetchone()
            payload = json.loads(previous['payload_json']) if previous else {}
            store.put('maintenance.cleanup', cleanup_id, {**payload, 'attempts': int(payload.get('attempts', 0)) + 1,
                      'run_id': run_id, 'last_error': None}, conn=local)
        return claimed

    def mark_cleanup_phase(self, cleanup_id: int, phase: str) -> None:
        if phase not in CLEANUP_EXECUTION_PHASES:
            raise ValueError('Unsupported cleanup phase')
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            conn.execute(
                text(
                    """
                    UPDATE document_cleanup_queue SET phase=:phase,
                        updated_at=CURRENT_TIMESTAMP WHERE cleanup_id=:cleanup_id
                    """
                ),
                {"cleanup_id": cleanup_id, "phase": phase},
            )

    def validate_cleanup_document(self, item: Mapping[str, Any]) -> None:
        """Revalidate the saved file facts before removing managed derivatives."""
        with self.engine.connect() as conn:
            current = self.catalog._snapshot(conn, item['md5'])
        expected = item['evidence_json']['execution_document']
        if current is None or any(current[key] != expected[key] for key in
                                  ('publication_id', 'document_revision', 'source_revision')):
            raise CatalogConflict('Cleanup file facts changed before storage cleanup; inspect the plan')

    def mark_cleanup_completed(self, cleanup_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            conn.execute(
                text(
                    """
                    UPDATE document_cleanup_queue SET status='completed', phase='completed',
                        completed_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP WHERE cleanup_id=:cleanup_id
                    """
                ),
                {"cleanup_id": cleanup_id},
            )

        self._clear_cleanup_error(cleanup_id)

    def mark_cleanup_failed(self, cleanup_id: int, error: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            conn.execute(
                text(
                    """
                    UPDATE document_cleanup_queue SET status='failed',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE cleanup_id=:cleanup_id AND status IN ('planned','running','failed')
                    """
                ),
                {"cleanup_id": cleanup_id},
            )
        store = configured_store()
        previous = store.get("maintenance.cleanup", cleanup_id) or {}
        store.put("maintenance.cleanup", cleanup_id, {**previous, "last_error": str(error)[:4000]})

    def mark_cleanup_canceled(self, cleanup_id: int, reason: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("SET TRANSACTION READ WRITE"))
            conn.execute(
                text(
                    """
                    UPDATE document_cleanup_queue SET status='canceled',
                        phase='canceled',
                        evidence_json=evidence_json || jsonb_build_object(
                            'cancellation', :reason
                        ),
                        completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE cleanup_id=:cleanup_id
                    """
                ),
                {"cleanup_id": cleanup_id, "reason": str(reason)[:1000]},
            )

        self._clear_cleanup_error(cleanup_id)

    @staticmethod
    def _clear_cleanup_error(cleanup_id):
        store = configured_store()
        previous = store.get("maintenance.cleanup", cleanup_id) or {}
        store.put("maintenance.cleanup", cleanup_id, {**previous, "last_error": None})

    def save_discovered_documents(self, commands) -> dict[str, Any]:
        with self.write_transaction() as conn:
            return self.catalog.sync_documents(conn, commands)

    def repair_filename_catalog(self, item: Mapping[str, Any]) -> None:
        """Checkpoint only the source locator after a verified filename-only move."""
        with self.write_transaction() as conn:
            persisted = conn.execute(text('''SELECT * FROM document_cleanup_queue
                WHERE cleanup_id=:id FOR UPDATE'''), {'id': item['cleanup_id']}).mappings().one()
            original = cleanup_source_path(persisted)
            if persisted['reason'] != 'filename_newlines' or persisted['phase'] != CLEANUP_PHASE_DATABASE or persisted['status'] != 'running':
                raise CatalogConflict('Filename repair requires a claimed database-phase plan')
            md5 = document_md5(persisted['md5'])
            self.catalog._lock(conn, md5)
            current = self.catalog._snapshot(conn, md5)
            expected = persisted['evidence_json'].get('execution_document')
            if current is None:
                if expected is not None:
                    raise CatalogConflict('Filename repair document disappeared; inspect the plan')
                return
            target = source_path(persisted['target_path'])
            if current['ya_resource_id'] not in (None, persisted['source_resource_id']):
                raise CatalogConflict('Filename repair source identity changed; inspect the plan')
            if current['ya_path'] == target:
                return
            if (expected is None or any(current[key] != expected[key] for key in
                                       ('publication_id', 'document_revision', 'source_revision'))
                    or str(current['ya_path']).removeprefix('disk:') != original):
                raise CatalogConflict('Filename repair catalog facts changed; inspect the plan')
            before = dict(conn.execute(text('''SELECT * FROM catalog_locations
                WHERE md5=:md5 AND provider='yandex' AND purpose='source' '''), {'md5': md5}).mappings().one())
            update_records(conn, 'source', [{**before, 'source_path': target}], {before['location_id']: before})

    def validate_publication(self, expected: Mapping[str, Any]) -> None:
        with self.engine.connect() as conn:
            self.catalog.validate_publication(conn, expected)

    def delete_document_state(self, md5: str, *, expected: Mapping[str, Any]) -> None:
        with self.write_transaction() as conn:
            self.lock_isbn_reviews_in_transaction(conn)
            self.catalog.delete_document(conn, md5, expected=expected)
            self.reconcile_pending_reviews_in_locked_transaction(conn)


__all__ = ["MonocorpusSyncRepository"]
