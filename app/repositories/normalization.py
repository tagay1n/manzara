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
                    c.surname_full,
                    c.surname_initials,
                    c.name_full,
                    c.name_initials,
                    c.father_name_full,
                    c.father_name_initials,
                    c.title,
                    c.sex,
                    c.identity_key,
                    c.created_at,
                    c.updated_at,
                    COALESCE(a.linked_aliases,0) AS linked_aliases
                FROM normalization_canonicals c
                LEFT JOIN (SELECT canonical_id,COUNT(*) AS linked_aliases
                    FROM normalization_aliases WHERE decision_status='linked' GROUP BY canonical_id) a
                    ON a.canonical_id = c.canonical_id
                {where}
                ORDER BY linked_aliases DESC, c.display_name ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_personality_source_documents(self) -> List[Dict[str, Any]]:
        """Project just the included people and language hints needed by the task."""
        roles = ('author', 'editor', 'translator', 'illustrator', 'contributor')
        people = ",".join(
            f"""'{role}', COALESCE(jsonb_agg(jsonb_build_object(
                '@type','Person','name',n.raw_name)
                ORDER BY c.position,c.nested_position)
                FILTER (WHERE c.role='{role}' AND n.kind='person'),'[]'::jsonb)"""
            for role in roles
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT d.md5,jsonb_build_object(
                        'inLanguage',COALESCE((SELECT jsonb_agg(l.language ORDER BY l.position)
                            FROM catalog_publication_languages l WHERE l.publication_id=p.publication_id),
                            '[]'::jsonb),{people}) AS schema_org
                    FROM catalog_documents d JOIN catalog_publications p USING(publication_id)
                    LEFT JOIN catalog_contributions c ON c.publication_id=p.publication_id
                        AND c.role IN ('author','editor','translator','illustrator','contributor')
                    LEFT JOIN catalog_names n USING(name_id)
                    WHERE p.inclusion='included' AND p.has_metadata AND p.metadata_present
                    GROUP BY d.md5,p.publication_id ORDER BY d.md5"""
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
                     RETURNING canonical_id""",
                    (entity_type, display_name, normalized_name, notes, now, now),
                )
                canonical_id = int(cur.scalar())
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
                conn.execute("SELECT set_config('manzara.catalog_actor','owner',true)")
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
                conn.execute("SELECT set_config('manzara.catalog_actor','owner',true)")
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
                conn.execute("SELECT set_config('manzara.catalog_actor','owner',true)")
                conn.execute(
                    """SELECT catalog_upsert('normalization_canonicals', jsonb_build_object('canonical_id', ?, 'entity_type', ?, 'display_name', ?, 'normalized_name', ?, 'status', ?, 'merged_into_id', ?, 'notes', ?, 'created_at', ?, 'updated_at', ?), ARRAY['canonical_id']::text[], ARRAY['entity_type','display_name','normalized_name','status','merged_into_id','notes','updated_at']::text[], ARRAY[]::text[])""",
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

    def list_normalization_aliases_for_canonical(
        self, entity_type: str, canonical_id: int
    ) -> List[Dict[str, Any]]:
        """Return retained aliases linked to one canonical entity."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM normalization_aliases
                WHERE entity_type = ? AND canonical_id = ? AND decision_status = 'linked'
                ORDER BY docs_count DESC, mentions_count DESC, raw_name ASC
                """,
                (entity_type, int(canonical_id)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_personality_checkpoint(self, raw_name: str) -> Optional[Dict[str, Any]]:
        """Return the durable per-exact-name normalization checkpoint."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM personality_normalization_checkpoints WHERE raw_name=?",
                (raw_name,),
            ).fetchone()
        return dict(row) if row else None

    def list_personality_checkpoints(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM personality_normalization_checkpoints ORDER BY raw_name"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_personality_decision_counts(self) -> Dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("""SELECT state, COUNT(*) AS count
                FROM personality_normalization_checkpoints
                WHERE state IN ('not_person', 'unusable', 'failed', 'deferred', 'retry_requested')
                GROUP BY state""").fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}

    def list_personality_decisions(self, *, state: str = "all", page: int = 1) -> Dict[str, Any]:
        states = ("not_person", "unusable", "failed", "deferred", "retry_requested")
        if state not in (*states, "all"):
            raise ValueError("unsupported personality decision state")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("page must be a positive integer")
        selected = list(states) if state == "all" else [state]
        with self._connect() as conn:
            rows = conn.execute("""SELECT raw_name, state, failure_context AS reason,
                document_count, mention_count, source_roles, updated_at, retryable
                FROM personality_normalization_checkpoints WHERE state = ANY(?)
                ORDER BY updated_at DESC, raw_name LIMIT 41 OFFSET ?""",
                (selected, (page - 1) * 40)).fetchall()
        return {"items": [dict(row) for row in rows[:40]], "page": page,
                "page_size": 40, "has_more": len(rows) > 40}

    def retry_personality_decision(self, *, raw_name: str, updated_at: str) -> Dict[str, Any]:
        """Optimistically reopen an explicitly reviewed negative decision only."""
        with self._lock:
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM personality_normalization_checkpoints WHERE raw_name=? FOR UPDATE",
                                   (raw_name,)).fetchone()
                if row is None or row["updated_at"] != updated_at or row["state"] not in {"not_person", "unusable"}:
                    raise ValueError("personality decision conflict; reload and try again")
                conn.execute("""UPDATE personality_normalization_checkpoints
                    SET state='retry_requested', retryable=TRUE, completed_at=NULL, updated_at=?
                    WHERE raw_name=?""", (utc_now(), raw_name))
        return {"raw_name": raw_name, "state": "retry_requested"}

    def save_personality_checkpoint(
        self,
        *,
        raw_name: str,
        source_fingerprint: str,
        document_count: int,
        mention_count: int,
        source_roles: list[str],
        prompt_version: str,
        schema_version: str,
        state: str,
        attempted_models: Dict[str, Any] | None = None,
        failure_context: str | None = None,
        retryable: bool = False,
        canonical_id: int | None = None,
        completed: bool = False,
    ) -> None:
        """Upsert one resumable checkpoint in PostgreSQL immediately."""
        if state in {"not_person", "unusable"}:
            if canonical_id is not None or retryable or not isinstance(failure_context, str) or not failure_context.strip():
                raise ValueError("negative personality decisions require a reason and no canonical or automatic retry")
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO personality_normalization_checkpoints (
                        raw_name, source_fingerprint, document_count, mention_count,
                        source_roles, prompt_version, schema_version, state,
                        attempted_models, failure_context, retryable, canonical_id,
                        updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?::jsonb, ?, ?, ?, ?::jsonb, ?, ?, ?, ?, ?)
                    ON CONFLICT(raw_name) DO UPDATE SET
                        source_fingerprint=excluded.source_fingerprint,
                        document_count=excluded.document_count,
                        mention_count=excluded.mention_count,
                        source_roles=excluded.source_roles,
                        prompt_version=excluded.prompt_version,
                        schema_version=excluded.schema_version,
                        state=excluded.state,
                        attempted_models=excluded.attempted_models,
                        failure_context=excluded.failure_context,
                        retryable=excluded.retryable,
                        canonical_id=excluded.canonical_id,
                        updated_at=excluded.updated_at,
                        completed_at=excluded.completed_at
                    """,
                    (
                        raw_name, source_fingerprint, int(document_count), int(mention_count),
                        json.dumps(sorted(set(source_roles)), ensure_ascii=False), prompt_version,
                        schema_version, state, json.dumps(attempted_models or {}, ensure_ascii=False),
                        failure_context, bool(retryable), canonical_id, now, now if completed else None,
                    ),
                )

    def persist_personality_normalization(
        self,
        *,
        raw_name: str,
        source_fingerprint: str,
        document_count: int,
        mention_count: int,
        source_roles: list[str],
        components: Dict[str, Any],
        display_name: str,
        identity_key: str,
        model: str,
        prompt_version: str,
        schema_version: str,
    ) -> Dict[str, Any]:
        """Write normalized catalog hypotheses through the shared engine."""
        from app.catalog.repository import CatalogRepository

        if self._engine is None:
            raise RuntimeError("Catalog normalization requires the shared PostgreSQL engine")
        return CatalogRepository(self._engine, schema=self.schema).normalize_personality(
            raw_name=raw_name, source_fingerprint=source_fingerprint,
            document_count=document_count, mention_count=mention_count,
            source_roles=source_roles, components=components, display_name=display_name,
            identity_key=identity_key, model=model, prompt_version=prompt_version,
            schema_version=schema_version,
        )

    def create_normalization_group(
        self,
        entity_type: str,
        display_name: str,
        normalized_name: str,
        alias_snapshots: List[Dict[str, Any]],
        *,
        suggestion_ids: List[int] | None = None,
    ) -> Dict[str, Any]:
        """Atomically create a canonical and retain raw names as linked aliases."""
        names = [str(item["raw_name"]) for item in alias_snapshots]
        placeholders = ", ".join("?" for _ in names)
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute("SELECT set_config('manzara.catalog_actor','owner',true)")
                existing = conn.execute(
                    f"""
                    SELECT raw_name, canonical_id FROM normalization_aliases
                    WHERE entity_type = ? AND raw_name IN ({placeholders})
                      AND decision_status = 'linked' AND canonical_id IS NOT NULL
                    FOR UPDATE
                    """,
                    (entity_type, *names),
                ).fetchall()
                if existing:
                    conflicts = ", ".join(str(row["raw_name"]) for row in existing)
                    raise ValueError(f"Aliases already linked to another canonical: {conflicts}")

                cur = conn.execute(
                    """
                    INSERT INTO normalization_canonicals (
                        entity_type, display_name, normalized_name, status,
                        merged_into_id, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, 'active', NULL, '', ?, ?)
                     RETURNING canonical_id""",
                    (entity_type, display_name, normalized_name, now, now),
                )
                canonical_id = int(cur.scalar())
                before_aliases = conn.execute(
                    f"SELECT * FROM normalization_aliases WHERE entity_type = ? AND raw_name IN ({placeholders})",
                    (entity_type, *names),
                ).fetchall()

                for item in alias_snapshots:
                    conn.execute(
                        """SELECT catalog_upsert('normalization_aliases', jsonb_build_object('entity_type', ?, 'raw_name', ?, 'normalized_name', ?, 'script_label', ?, 'docs_count', ?, 'mentions_count', ?, 'marker_count', ?, 'decision_status', 'linked', 'canonical_id', ?, 'confidence', 1.0, 'source', 'manual_group', 'reason', 'group_create', 'created_at', ?, 'updated_at', ?), ARRAY['entity_type','raw_name']::text[], ARRAY['normalized_name','script_label','docs_count','mentions_count','marker_count','decision_status','canonical_id','confidence','source','reason','updated_at']::text[], ARRAY[]::text[])""",
                        (
                            entity_type,
                            item["raw_name"],
                            item["normalized_name"],
                            item["script_label"],
                            int(item["docs_count"]),
                            int(item["mentions_count"]),
                            int(item["marker_count"]),
                            canonical_id,
                            now,
                            now,
                        ),
                    )

                ids = [int(value) for value in (suggestion_ids or [])]
                if ids:
                    id_placeholders = ", ".join("?" for _ in ids)
                    conn.execute(
                        f"UPDATE normalization_suggestions SET status='accepted', updated_at=? WHERE entity_type=? AND suggestion_id IN ({id_placeholders})",
                        (now, entity_type, *ids),
                    )

                after_aliases = conn.execute(
                    f"SELECT * FROM normalization_aliases WHERE entity_type = ? AND raw_name IN ({placeholders}) ORDER BY raw_name",
                    (entity_type, *names),
                ).fetchall()
                payload = {
                    "created_canonical_id": canonical_id,
                    "raw_names": names,
                    "before_aliases": [dict(row) for row in before_aliases],
                    "after_aliases": [dict(row) for row in after_aliases],
                    "suggestion_ids": ids,
                }
                event_cur = conn.execute(
                    """INSERT INTO normalization_events
                       (entity_type, action, payload_json, reverted, created_at)
                       VALUES (?, 'create_canonical_group', ?, 0, ?)""",
                    (entity_type, json.dumps(payload, ensure_ascii=False), now),
                )
                canonical = conn.execute(
                    "SELECT * FROM normalization_canonicals WHERE canonical_id = ?",
                    (canonical_id,),
                ).fetchone()
                event_id = int(event_cur.lastrowid)
        return {
            "canonical": dict(canonical),
            "aliases": [dict(row) for row in after_aliases],
            "event": {
                "event_id": event_id,
                "entity_type": entity_type,
                "action": "create_canonical_group",
                "payload": payload,
                "reverted": False,
                "created_at": now,
            },
        }

    def link_normalization_alias_group(
        self,
        entity_type: str,
        canonical_id: int,
        alias_snapshots: List[Dict[str, Any]],
        *,
        suggestion_ids: List[int] | None = None,
    ) -> Dict[str, Any]:
        """Atomically attach raw aliases without stealing them from another entity."""
        names = [str(item["raw_name"]) for item in alias_snapshots]
        placeholders = ", ".join("?" for _ in names)
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute("SELECT set_config('manzara.catalog_actor','owner',true)")
                canonical = conn.execute(
                    "SELECT * FROM normalization_canonicals WHERE canonical_id=? FOR UPDATE",
                    (int(canonical_id),),
                ).fetchone()
                if not canonical or str(canonical["entity_type"]) != entity_type:
                    raise ValueError("Canonical not found for entity type")
                if str(canonical["status"]) != "active":
                    raise ValueError("Canonical is not active")
                before_aliases = conn.execute(
                    f"SELECT * FROM normalization_aliases WHERE entity_type=? AND raw_name IN ({placeholders}) FOR UPDATE",
                    (entity_type, *names),
                ).fetchall()
                conflicts = [
                    str(row["raw_name"])
                    for row in before_aliases
                    if str(row.get("decision_status") or "") == "linked"
                    and int(row.get("canonical_id") or 0) != int(canonical_id)
                ]
                if conflicts:
                    raise ValueError(
                        "Aliases already linked to another canonical: " + ", ".join(conflicts)
                    )
                for item in alias_snapshots:
                    conn.execute(
                        """SELECT catalog_upsert('normalization_aliases', jsonb_build_object('entity_type', ?, 'raw_name', ?, 'normalized_name', ?, 'script_label', ?, 'docs_count', ?, 'mentions_count', ?, 'marker_count', ?, 'decision_status', 'linked', 'canonical_id', ?, 'confidence', 1.0, 'source', 'manual_bulk', 'reason', 'bulk_link', 'created_at', ?, 'updated_at', ?), ARRAY['entity_type','raw_name']::text[], ARRAY['normalized_name','script_label','docs_count','mentions_count','marker_count','decision_status','canonical_id','confidence','source','reason','updated_at']::text[], ARRAY[]::text[])""",
                        (
                            entity_type,
                            item["raw_name"],
                            item["normalized_name"],
                            item["script_label"],
                            int(item["docs_count"]),
                            int(item["mentions_count"]),
                            int(item["marker_count"]),
                            int(canonical_id),
                            now,
                            now,
                        ),
                    )
                ids = [int(value) for value in (suggestion_ids or [])]
                if ids:
                    id_placeholders = ", ".join("?" for _ in ids)
                    conn.execute(
                        f"UPDATE normalization_suggestions SET status='accepted', updated_at=? WHERE entity_type=? AND suggestion_id IN ({id_placeholders})",
                        (now, entity_type, *ids),
                    )
                after_aliases = conn.execute(
                    f"SELECT * FROM normalization_aliases WHERE entity_type=? AND raw_name IN ({placeholders}) ORDER BY raw_name",
                    (entity_type, *names),
                ).fetchall()
                payload = {
                    "canonical_id": int(canonical_id),
                    "raw_names": names,
                    "before_aliases": [dict(row) for row in before_aliases],
                    "after_aliases": [dict(row) for row in after_aliases],
                    "suggestion_ids": ids,
                }
                cur = conn.execute(
                    """INSERT INTO normalization_events
                       (entity_type, action, payload_json, reverted, created_at)
                       VALUES (?, 'bulk_link_aliases', ?, 0, ?)""",
                    (entity_type, json.dumps(payload, ensure_ascii=False), now),
                )
                event_id = int(cur.lastrowid)
        return {
            "updated": len(after_aliases),
            "aliases": [dict(row) for row in after_aliases],
            "event": {"event_id": event_id, "entity_type": entity_type, "action": "bulk_link_aliases", "payload": payload, "reverted": False, "created_at": now},
        }

    def rename_normalization_canonical(
        self,
        entity_type: str,
        canonical_id: int,
        display_name: str,
        normalized_name: str,
    ) -> Dict[str, Any]:
        """Atomically rename a canonical and append a reversible event."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute("SELECT set_config('manzara.catalog_actor','owner',true)")
                before = conn.execute(
                    "SELECT * FROM normalization_canonicals WHERE canonical_id=? FOR UPDATE",
                    (int(canonical_id),),
                ).fetchone()
                if not before or str(before["entity_type"]) != entity_type:
                    raise ValueError("Canonical not found for entity type")
                if str(before["status"]) != "active":
                    raise ValueError("Canonical is not active")
                conn.execute(
                    "UPDATE normalization_canonicals SET display_name=?, normalized_name=?, updated_at=? WHERE canonical_id=?",
                    (display_name, normalized_name, now, int(canonical_id)),
                )
                after = conn.execute(
                    "SELECT * FROM normalization_canonicals WHERE canonical_id=?",
                    (int(canonical_id),),
                ).fetchone()
                payload = {"canonical_id": int(canonical_id), "before": dict(before), "after": dict(after)}
                cur = conn.execute(
                    """INSERT INTO normalization_events
                       (entity_type, action, payload_json, reverted, created_at)
                       VALUES (?, 'rename_canonical', ?, 0, ?)""",
                    (entity_type, json.dumps(payload, ensure_ascii=False), now),
                )
                event_id = int(cur.lastrowid)
        return {
            "canonical": dict(after),
            "event": {"event_id": event_id, "entity_type": entity_type, "action": "rename_canonical", "payload": payload, "reverted": False, "created_at": now},
        }

    def apply_publisher_change_set(self, change_set: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a publisher workbench draft in one durable transaction.

        The workbench deliberately uses exact retained raw names only.  This
        method owns every write so a conflict rolls back the whole draft.
        """
        now = utc_now()

        def alias(conn: Any, raw_name: str, canonical_id: int, reason: str) -> None:
            name = str(raw_name).strip()
            existing = conn.execute(
                """SELECT canonical_id, decision_status FROM normalization_aliases
                   WHERE entity_type='publisher' AND raw_name=? FOR UPDATE""",
                (name,),
            ).fetchone()
            if (
                existing
                and str(existing["decision_status"]) == "linked"
                and int(existing["canonical_id"] or 0) != canonical_id
            ):
                raise ValueError("Publisher alias is already linked to another canonical")
            conn.execute(
                """SELECT catalog_upsert('normalization_aliases', jsonb_build_object('entity_type', 'publisher', 'raw_name', ?, 'normalized_name', ?, 'script_label', 'other', 'docs_count', 0, 'mentions_count', 0, 'marker_count', 0, 'decision_status', 'linked', 'canonical_id', ?, 'confidence', 1.0, 'source', 'publisher_batch', 'reason', ?, 'created_at', ?, 'updated_at', ?), ARRAY['entity_type','raw_name']::text[], ARRAY['decision_status','canonical_id','confidence','source','reason','updated_at']::text[], ARRAY[]::text[])""",
                (name, name.casefold(), canonical_id, reason, now, now),
            )

        def active_canonical(conn: Any, canonical_id: int) -> Dict[str, Any]:
            row = conn.execute(
                """SELECT * FROM normalization_canonicals
                   WHERE canonical_id=? AND entity_type='publisher' FOR UPDATE""",
                (canonical_id,),
            ).fetchone()
            if not row or str(row["status"]) != "active":
                raise ValueError("Publisher canonical is missing or inactive")
            return dict(row)

        with self._lock:
            with self._connect() as conn:
                conn.execute("SELECT set_config('manzara.catalog_actor','owner',true)")
                draft = None
                if change_set.get("use_draft"):
                    draft = self._prepare_publisher_draft_apply(conn, change_set["revision"])
                    change_set = draft["changes"]
                identity_mapping = {}
                before = {
                    "canonicals": [dict(row) for row in conn.execute("SELECT * FROM normalization_canonicals WHERE entity_type='publisher' ORDER BY canonical_id FOR UPDATE").fetchall()],
                    "aliases": [dict(row) for row in conn.execute("SELECT * FROM normalization_aliases WHERE entity_type='publisher' ORDER BY alias_id FOR UPDATE").fetchall()],
                }
                touched: set[int] = set()
                for rename in change_set["renames"]:
                    canonical = active_canonical(conn, int(rename["canonical_id"]))
                    canonical_id = int(canonical["canonical_id"])
                    old_name = str(canonical["display_name"]).strip()
                    new_name = str(rename["display_name"]).strip()
                    conn.execute(
                        "UPDATE normalization_canonicals SET display_name=?, normalized_name=?, updated_at=? WHERE canonical_id=?",
                        (new_name, new_name.casefold(), now, canonical_id),
                    )
                    alias(conn, old_name, canonical_id, "rename")
                    alias(conn, new_name, canonical_id, "rename")
                    touched.add(canonical_id)

                for raw_name in change_set["keeps"]:
                    existing = conn.execute(
                        "SELECT * FROM normalization_aliases WHERE entity_type='publisher' AND raw_name=? FOR UPDATE",
                        (raw_name,),
                    ).fetchone()
                    if existing and str(existing["decision_status"]) == "linked":
                        raise ValueError("Raw publisher name is already linked")
                    cur = conn.execute(
                        """INSERT INTO normalization_canonicals
                           (entity_type, display_name, normalized_name, status, merged_into_id, notes, created_at, updated_at)
                           VALUES ('publisher', ?, ?, 'active', NULL, '', ?, ?) RETURNING canonical_id""",
                        (raw_name, raw_name.casefold(), now, now),
                    )
                    canonical_id = int(cur.scalar())
                    alias(conn, raw_name, canonical_id, "keep")
                    identity_mapping[f"raw:{raw_name}"] = f"canonical:{canonical_id}"
                    touched.add(canonical_id)

                for merge in change_set["merges"]:
                    canonical_rows = [active_canonical(conn, int(value)) for value in merge["canonical_ids"]]
                    target_id = min((int(row["canonical_id"]) for row in canonical_rows), default=0)
                    if not target_id:
                        cur = conn.execute(
                            """INSERT INTO normalization_canonicals
                               (entity_type, display_name, normalized_name, status, merged_into_id, notes, created_at, updated_at)
                               VALUES ('publisher', ?, ?, 'active', NULL, '', ?, ?) RETURNING canonical_id""",
                            (merge["display_name"], str(merge["display_name"]).casefold(), now, now),
                        )
                        target_id = int(cur.scalar())
                    for raw_name in merge["raw_names"]:
                        existing = conn.execute(
                            "SELECT * FROM normalization_aliases WHERE entity_type='publisher' AND raw_name=? FOR UPDATE",
                            (raw_name,),
                        ).fetchone()
                        if existing and str(existing["decision_status"]) == "linked":
                            raise ValueError("Raw publisher name is already linked")
                    for canonical in canonical_rows:
                        source_id = int(canonical["canonical_id"])
                        conn.execute(
                            "UPDATE normalization_aliases SET canonical_id=?, updated_at=? WHERE entity_type='publisher' AND canonical_id=? AND decision_status='linked'",
                            (target_id, now, source_id),
                        )
                        alias(conn, str(canonical["display_name"]), target_id, "merge")
                        if source_id != target_id:
                            conn.execute(
                                "UPDATE normalization_canonicals SET status='merged', merged_into_id=?, updated_at=? WHERE canonical_id=?",
                                (target_id, now, source_id),
                            )
                    final_name = str(merge["display_name"]).strip()
                    conn.execute(
                        "UPDATE normalization_canonicals SET display_name=?, normalized_name=?, updated_at=? WHERE canonical_id=?",
                        (final_name, final_name.casefold(), now, target_id),
                    )
                    for raw_name in merge["raw_names"]:
                        alias(conn, raw_name, target_id, "merge")
                    alias(conn, final_name, target_id, "merge")
                    for source_id in merge["canonical_ids"]:
                        identity_mapping[f"canonical:{source_id}"] = f"canonical:{target_id}"
                    for raw_name in merge["raw_names"]:
                        identity_mapping[f"raw:{raw_name}"] = f"canonical:{target_id}"
                    touched.add(target_id)

                self._reconcile_publisher_separations(conn, identity_mapping)
                if draft is not None:
                    self._finish_publisher_draft_apply(conn, draft)
                after = {
                    "canonicals": [dict(row) for row in conn.execute("SELECT * FROM normalization_canonicals WHERE entity_type='publisher' ORDER BY canonical_id").fetchall()],
                    "aliases": [dict(row) for row in conn.execute("SELECT * FROM normalization_aliases WHERE entity_type='publisher' ORDER BY alias_id").fetchall()],
                }
                event_payload = {"change_set": change_set, "before": before, "after": after, "touched_canonical_ids": sorted(touched), "proposal_ids": draft["proposal_ids"] if draft is not None else []}
                cur = conn.execute(
                    """INSERT INTO normalization_events (entity_type, action, payload_json, reverted, created_at)
                       VALUES ('publisher', 'apply_publisher_change_set', ?, 0, ?)""",
                    (json.dumps(event_payload, ensure_ascii=False), now),
                )
                event_id = int(cur.lastrowid)
        return {"ok": True, "event_id": event_id, "touched_canonical_ids": sorted(touched)}

    def apply_personality_change_set(self, change_set: Dict[str, Any]) -> Dict[str, Any]:
        """Atomically rename or exact-manually-merge active canonical people."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute("SELECT set_config('manzara.catalog_actor','owner',true)")
                def canonical(canonical_id: int) -> Dict[str, Any]:
                    row = conn.execute("SELECT * FROM normalization_canonicals WHERE canonical_id=? AND entity_type='personality' FOR UPDATE", (canonical_id,)).fetchone()
                    if not row or row["status"] != "active":
                        raise ValueError("personality canonical is missing or inactive")
                    return dict(row)

                def retain_alias(name: str, canonical_id: int, reason: str) -> None:
                    conn.execute("""SELECT catalog_upsert('normalization_aliases', jsonb_build_object('entity_type', 'personality', 'raw_name', ?, 'normalized_name', ?, 'script_label', 'other', 'docs_count', 0, 'mentions_count', 0, 'marker_count', 0, 'decision_status', 'linked', 'canonical_id', ?, 'confidence', 1.0, 'source', 'personality_workbench', 'reason', ?, 'source_roles', '[]'::jsonb, 'successful_model', 'manual', 'created_at', ?, 'updated_at', ?), ARRAY['entity_type','raw_name']::text[], ARRAY['canonical_id','decision_status','source','reason','successful_model','updated_at']::text[], ARRAY['successful_model']::text[])""",
                        (name, name.casefold(), canonical_id, reason, now, now))

                touched: set[int] = set()
                component_fields = (
                    "surname_full",
                    "surname_initials",
                    "name_full",
                    "name_initials",
                    "father_name_full",
                    "father_name_initials",
                    "title",
                    "sex",
                )
                for correction in change_set.get("corrections", []):
                    row = canonical(int(correction["canonical_id"]))
                    canonical_id = int(row["canonical_id"])
                    old = str(row["display_name"])
                    components = correction["components"]
                    assignments = ", ".join(
                        f"{field}=?" for field in component_fields
                    )
                    conn.execute(
                        f"""UPDATE normalization_canonicals
                            SET {assignments}, display_name=?, normalized_name=?,
                                identity_key=?, updated_at=?
                            WHERE canonical_id=?""",
                        (
                            *[
                                components.get(field)
                                for field in component_fields
                            ],
                            correction["display_name"],
                            correction["identity_key"],
                            correction["identity_key"],
                            now,
                            canonical_id,
                        ),
                    )
                    if old != correction["display_name"]:
                        retain_alias(
                            old, canonical_id, "structured_correction"
                        )
                    touched.add(canonical_id)
                for rename in change_set["renames"]:
                    row = canonical(int(rename["canonical_id"]))
                    canonical_id = int(row["canonical_id"])
                    old = str(row["display_name"])
                    new = str(rename["display_name"])
                    conn.execute("UPDATE normalization_canonicals SET display_name=?, normalized_name=?, updated_at=? WHERE canonical_id=?", (new, new.casefold(), now, canonical_id))
                    retain_alias(old, canonical_id, "rename")
                    retain_alias(new, canonical_id, "rename")
                    touched.add(canonical_id)
                for merge in change_set["merges"]:
                    rows = [canonical(int(value)) for value in merge["canonical_ids"]]
                    target_id = min(int(row["canonical_id"]) for row in rows)
                    for row in rows:
                        source_id = int(row["canonical_id"])
                        conn.execute("UPDATE normalization_aliases SET canonical_id=?, updated_at=? WHERE entity_type='personality' AND canonical_id=? AND decision_status='linked'", (target_id, now, source_id))
                        retain_alias(str(row["display_name"]), target_id, "merge")
                        if source_id != target_id:
                            conn.execute("UPDATE normalization_canonicals SET status='merged', merged_into_id=?, updated_at=? WHERE canonical_id=?", (target_id, now, source_id))
                    final_name = str(merge["display_name"])
                    conn.execute("UPDATE normalization_canonicals SET display_name=?, normalized_name=?, updated_at=? WHERE canonical_id=?", (final_name, final_name.casefold(), now, target_id))
                    retain_alias(final_name, target_id, "merge")
                    touched.add(target_id)
                cur = conn.execute("INSERT INTO normalization_events (entity_type, action, payload_json, reverted, created_at) VALUES ('personality', 'apply_personality_change_set', ?, 0, ?)", (json.dumps(change_set, ensure_ascii=False), now))
                event_id = int(cur.lastrowid)
        return {"ok": True, "event_id": event_id, "touched_canonical_ids": sorted(touched)}

    def dismiss_normalization_suggestion(
        self, entity_type: str, suggestion_id: int
    ) -> Dict[str, Any]:
        """Dismiss a proposal without rejecting its raw alias."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM normalization_suggestions WHERE suggestion_id=? AND entity_type=? FOR UPDATE",
                    (int(suggestion_id), entity_type),
                ).fetchone()
                if not row:
                    raise ValueError("Suggestion not found for entity type")
                if str(row["status"]) != "open":
                    raise ValueError("Suggestion is not open")
                conn.execute(
                    "UPDATE normalization_suggestions SET status='dismissed', updated_at=? WHERE suggestion_id=?",
                    (now, int(suggestion_id)),
                )
        return {**dict(row), "status": "dismissed", "updated_at": now}


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
                    """SELECT catalog_upsert('normalization_aliases', jsonb_build_object('entity_type', ?, 'raw_name', ?, 'normalized_name', ?, 'script_label', ?, 'docs_count', ?, 'mentions_count', ?, 'marker_count', ?, 'decision_status', ?, 'canonical_id', ?, 'confidence', ?, 'source', ?, 'reason', ?, 'created_at', ?, 'updated_at', ?), ARRAY['entity_type','raw_name']::text[], ARRAY['normalized_name','script_label','docs_count','mentions_count','marker_count','decision_status','canonical_id','confidence','source','reason','updated_at']::text[], ARRAY[]::text[])""",
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
                conn.execute("SELECT set_config('manzara.catalog_actor','owner',true)")
                conn.execute(
                    """SELECT catalog_upsert('normalization_aliases', jsonb_build_object('alias_id', ?, 'entity_type', ?, 'raw_name', ?, 'normalized_name', ?, 'script_label', ?, 'docs_count', ?, 'mentions_count', ?, 'marker_count', ?, 'decision_status', ?, 'canonical_id', ?, 'confidence', ?, 'source', ?, 'reason', ?, 'created_at', ?, 'updated_at', ?), ARRAY['entity_type','raw_name']::text[], ARRAY['alias_id','normalized_name','script_label','docs_count','mentions_count','marker_count','decision_status','canonical_id','confidence','source','reason','updated_at']::text[], ARRAY[]::text[])""",
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
                conn.execute("SELECT set_config('manzara.catalog_actor','owner',true)")
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
                conn.execute("SELECT set_config('manzara.catalog_actor','owner',true)")
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
