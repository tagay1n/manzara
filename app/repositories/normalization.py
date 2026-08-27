from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.repositories.core import utc_now


class NormalizationRepository:
    """PostgreSQL operations for the normalization domain."""

    def list_normalization_canonicals(
        self,
        entity_type: str,
        *,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return canonical entities with linked-alias counters."""
        where = "WHERE c.entity_type = ?"
        params: List[Any] = [entity_type]
        if not include_inactive:
            where += " AND c.status = 'active'"

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    c.canonical_id,
                    c.entity_type,
                    c.display_name,
                    c.normalized_name,
                    c.status,
                    c.merged_into_id,
                    c.notes,
                    c.created_at,
                    c.updated_at,
                    COUNT(a.alias_id) AS linked_aliases
                FROM normalization_canonicals c
                LEFT JOIN normalization_aliases a
                    ON a.canonical_id = c.canonical_id
                   AND a.decision_status = 'linked'
                {where}
                GROUP BY c.canonical_id
                ORDER BY linked_aliases DESC, c.display_name ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]


    def get_normalization_canonical(self, canonical_id: int) -> Optional[Dict[str, Any]]:
        """Return one canonical entity by id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM normalization_canonicals WHERE canonical_id = ?",
                (canonical_id,),
            ).fetchone()
        return dict(row) if row else None


    def create_normalization_canonical(
        self,
        entity_type: str,
        display_name: str,
        normalized_name: str,
        *,
        notes: str = "",
    ) -> Dict[str, Any]:
        """Create a canonical entity."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO normalization_canonicals (
                        entity_type, display_name, normalized_name, status,
                        merged_into_id, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', NULL, ?, ?, ?)
                    """,
                    (entity_type, display_name, normalized_name, notes, now, now),
                )
                canonical_id = int(cur.lastrowid)
        canonical = self.get_normalization_canonical(canonical_id)
        if not canonical:
            raise RuntimeError("Failed to create canonical")
        return canonical


    def update_normalization_canonical(
        self,
        canonical_id: int,
        updates: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Patch canonical entity fields."""
        allowed = {"display_name", "normalized_name", "status", "merged_into_id", "notes"}
        fields = [field for field in updates.keys() if field in allowed]
        if not fields:
            return self.get_normalization_canonical(canonical_id)

        assignments: List[str] = []
        values: List[Any] = []
        for field in fields:
            assignments.append(f"{field} = ?")
            values.append(updates[field])
        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(canonical_id)

        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE normalization_canonicals
                    SET {", ".join(assignments)}
                    WHERE canonical_id = ?
                    """,
                    values,
                )
        return self.get_normalization_canonical(canonical_id)


    def delete_normalization_canonical(self, canonical_id: int) -> None:
        """Delete canonical entity row."""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM normalization_canonicals WHERE canonical_id = ?",
                    (canonical_id,),
                )


    def count_linked_aliases_for_canonical(self, canonical_id: int) -> int:
        """Return number of linked aliases for canonical entity."""
        with self._connect() as conn:
            value = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM normalization_aliases
                WHERE canonical_id = ? AND decision_status = 'linked'
                """,
                (canonical_id,),
            ).scalar()
        return int(value or 0)


    def restore_normalization_canonical_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> None:
        """Restore canonical row from snapshot, or no-op when snapshot is None."""
        if snapshot is None:
            return
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO normalization_canonicals (
                        canonical_id, entity_type, display_name, normalized_name,
                        status, merged_into_id, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(canonical_id) DO UPDATE SET
                        entity_type=excluded.entity_type,
                        display_name=excluded.display_name,
                        normalized_name=excluded.normalized_name,
                        status=excluded.status,
                        merged_into_id=excluded.merged_into_id,
                        notes=excluded.notes,
                        updated_at=excluded.updated_at
                    """,
                    (
                        snapshot.get("canonical_id"),
                        snapshot.get("entity_type") or "",
                        snapshot.get("display_name") or "",
                        snapshot.get("normalized_name") or "",
                        snapshot.get("status") or "active",
                        snapshot.get("merged_into_id"),
                        snapshot.get("notes"),
                        snapshot.get("created_at") or now,
                        now,
                    ),
                )


    def list_normalization_aliases(self, entity_type: str) -> List[Dict[str, Any]]:
        """Return saved alias resolutions for an entity type."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM normalization_aliases
                WHERE entity_type = ?
                ORDER BY docs_count DESC, mentions_count DESC, raw_name ASC
                """,
                (entity_type,),
            ).fetchall()
        return [dict(row) for row in rows]


    def get_normalization_alias(
        self,
        entity_type: str,
        raw_name: str,
    ) -> Optional[Dict[str, Any]]:
        """Return one alias resolution by entity_type + raw_name."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM normalization_aliases
                WHERE entity_type = ? AND raw_name = ?
                LIMIT 1
                """,
                (entity_type, raw_name),
            ).fetchone()
        return dict(row) if row else None


    def get_normalization_alias_by_id(self, alias_id: int) -> Optional[Dict[str, Any]]:
        """Return one alias resolution by id."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM normalization_aliases WHERE alias_id = ?",
                (alias_id,),
            ).fetchone()
        return dict(row) if row else None


    def upsert_normalization_alias(
        self,
        *,
        entity_type: str,
        raw_name: str,
        normalized_name: str,
        script_label: str,
        docs_count: int,
        mentions_count: int,
        marker_count: int,
        decision_status: str,
        canonical_id: Optional[int],
        confidence: Optional[float],
        source: Optional[str],
        reason: Optional[str],
    ) -> Dict[str, Any]:
        """Insert or update one alias resolution row."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO normalization_aliases (
                        entity_type, raw_name, normalized_name, script_label,
                        docs_count, mentions_count, marker_count,
                        decision_status, canonical_id, confidence, source, reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(entity_type, raw_name) DO UPDATE SET
                        normalized_name=excluded.normalized_name,
                        script_label=excluded.script_label,
                        docs_count=excluded.docs_count,
                        mentions_count=excluded.mentions_count,
                        marker_count=excluded.marker_count,
                        decision_status=excluded.decision_status,
                        canonical_id=excluded.canonical_id,
                        confidence=excluded.confidence,
                        source=excluded.source,
                        reason=excluded.reason,
                        updated_at=excluded.updated_at
                    """,
                    (
                        entity_type,
                        raw_name,
                        normalized_name,
                        script_label,
                        int(docs_count),
                        int(mentions_count),
                        int(marker_count),
                        decision_status,
                        canonical_id,
                        confidence,
                        source,
                        reason,
                        now,
                        now,
                    ),
                )
        alias = self.get_normalization_alias(entity_type, raw_name)
        if not alias:
            raise RuntimeError("Failed to upsert normalization alias")
        return alias


    def restore_normalization_alias_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> None:
        """Restore alias row from snapshot, or remove it when snapshot is None."""
        if snapshot is None:
            return
        entity_type = str(snapshot.get("entity_type") or "")
        raw_name = str(snapshot.get("raw_name") or "")
        if not entity_type or not raw_name:
            return
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO normalization_aliases (
                        alias_id, entity_type, raw_name, normalized_name, script_label,
                        docs_count, mentions_count, marker_count,
                        decision_status, canonical_id, confidence, source, reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(entity_type, raw_name) DO UPDATE SET
                        alias_id=excluded.alias_id,
                        normalized_name=excluded.normalized_name,
                        script_label=excluded.script_label,
                        docs_count=excluded.docs_count,
                        mentions_count=excluded.mentions_count,
                        marker_count=excluded.marker_count,
                        decision_status=excluded.decision_status,
                        canonical_id=excluded.canonical_id,
                        confidence=excluded.confidence,
                        source=excluded.source,
                        reason=excluded.reason,
                        updated_at=excluded.updated_at
                    """,
                    (
                        snapshot.get("alias_id"),
                        entity_type,
                        raw_name,
                        snapshot.get("normalized_name") or "",
                        snapshot.get("script_label") or "other",
                        int(snapshot.get("docs_count") or 0),
                        int(snapshot.get("mentions_count") or 0),
                        int(snapshot.get("marker_count") or 0),
                        snapshot.get("decision_status") or "pending",
                        snapshot.get("canonical_id"),
                        snapshot.get("confidence"),
                        snapshot.get("source"),
                        snapshot.get("reason"),
                        snapshot.get("created_at") or now,
                        now,
                    ),
                )


    def delete_normalization_alias(self, entity_type: str, raw_name: str) -> None:
        """Delete one alias row."""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    DELETE FROM normalization_aliases
                    WHERE entity_type = ? AND raw_name = ?
                    """,
                    (entity_type, raw_name),
                )


    def reassign_aliases_between_canonicals(
        self,
        *,
        entity_type: str,
        source_canonical_id: int,
        target_canonical_id: int,
    ) -> List[Dict[str, Any]]:
        """Move linked aliases from source canonical to target canonical."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM normalization_aliases
                    WHERE entity_type = ?
                      AND canonical_id = ?
                      AND decision_status = 'linked'
                    ORDER BY alias_id ASC
                    """,
                    (entity_type, source_canonical_id),
                ).fetchall()
                snapshots = [dict(row) for row in rows]
                conn.execute(
                    """
                    UPDATE normalization_aliases
                    SET canonical_id = ?, updated_at = ?
                    WHERE entity_type = ?
                      AND canonical_id = ?
                      AND decision_status = 'linked'
                    """,
                    (target_canonical_id, utc_now(), entity_type, source_canonical_id),
                )
        return snapshots


    def create_normalization_event(
        self,
        entity_type: str,
        action: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persist one normalization audit event row."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO normalization_events (
                        entity_type, action, payload_json, reverted, created_at
                    ) VALUES (?, ?, ?, 0, ?)
                    """,
                    (entity_type, action, json.dumps(payload, ensure_ascii=False), now),
                )
                event_id = int(cur.lastrowid)
        event = self.get_normalization_event(event_id)
        if not event:
            raise RuntimeError("Failed to create normalization event")
        return event


    def get_normalization_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        """Return one normalization event by id."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT event_id, entity_type, action, payload_json, reverted, created_at
                FROM normalization_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if not row:
            return None
        payload = dict(row)
        payload["reverted"] = bool(payload.get("reverted", 0))
        payload["payload"] = json.loads(payload.pop("payload_json") or "{}")
        return payload


    def list_normalization_events(
        self,
        entity_type: str,
        *,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return recent normalization events."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, entity_type, action, payload_json, reverted, created_at
                FROM normalization_events
                WHERE entity_type = ?
                ORDER BY event_id DESC
                LIMIT ?
                """,
                (entity_type, max(1, int(limit))),
            ).fetchall()
        items: List[Dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["reverted"] = bool(payload.get("reverted", 0))
            payload["payload"] = json.loads(payload.pop("payload_json") or "{}")
            items.append(payload)
        return items


    def mark_normalization_event_reverted(self, event_id: int) -> None:
        """Mark normalization event as reverted."""
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE normalization_events
                    SET reverted = 1
                    WHERE event_id = ?
                    """,
                    (event_id,),
                )


    def replace_open_suggestions(
        self,
        entity_type: str,
        suggestions: List[Dict[str, Any]],
    ) -> None:
        """Supersede previous open suggestions and insert a fresh set."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE normalization_suggestions
                    SET status = 'superseded', updated_at = ?
                    WHERE entity_type = ? AND status = 'open'
                    """,
                    (now, entity_type),
                )
                for item in suggestions:
                    conn.execute(
                        """
                        INSERT INTO normalization_suggestions (
                            entity_type, raw_name, normalized_name, target_canonical_id,
                            suggestion_kind, confidence, confidence_band,
                            model, rationale, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                        """,
                        (
                            entity_type,
                            item.get("raw_name") or "",
                            item.get("normalized_name") or "",
                            item.get("target_canonical_id"),
                            item.get("suggestion_kind") or "create",
                            float(item.get("confidence") or 0.0),
                            item.get("confidence_band") or "low",
                            item.get("model"),
                            item.get("rationale"),
                            now,
                            now,
                        ),
                    )


    def list_open_suggestions(
        self,
        entity_type: str,
        *,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return open suggestions for entity type."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM normalization_suggestions
                WHERE entity_type = ? AND status = 'open'
                ORDER BY confidence DESC, suggestion_id DESC
                LIMIT ?
                """,
                (entity_type, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]


    def update_suggestion_statuses(
        self,
        suggestion_ids: List[int],
        status: str,
    ) -> None:
        """Bulk update suggestion statuses."""
        ids = [int(item) for item in suggestion_ids if int(item) > 0]
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE normalization_suggestions
                    SET status = ?, updated_at = ?
                    WHERE suggestion_id IN ({placeholders})
                    """,
                    (status, utc_now(), *ids),
                )
