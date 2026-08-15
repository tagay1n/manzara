from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.repositories.core import _json_hash, _normalize_shayan_entries, utc_now

class ShayanRepository:
    """PostgreSQL operations for the shayan domain."""

    def list_shayan_manifest_entries(self) -> Dict[str, Dict[str, Any]]:
        """Return full Shayan manifest as key->payload map."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT entry_key, payload_json
                FROM shayan_manifest_entries
                ORDER BY entry_key ASC
                """
            ).fetchall()
        payload: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("entry_key") or "").strip()
            if not key:
                continue
            try:
                value = json.loads(str(row.get("payload_json") or "{}"))
            except Exception:
                value = {}
            payload[key] = value if isinstance(value, dict) else {}
        return payload


    def get_shayan_manifest_entry(self, entry_key: str) -> Optional[Dict[str, Any]]:
        """Return one Shayan manifest entry with decoded payload."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    entry_key,
                    payload_json,
                    payload_hash,
                    yadisk_status,
                    yadisk_remote_path,
                    yadisk_uploaded_payload_hash,
                    yadisk_uploaded_at,
                    yadisk_last_attempt_at,
                    yadisk_last_error,
                    webdav_status,
                    webdav_remote_path,
                    webdav_source_md5,
                    webdav_source_size,
                    webdav_target_etag,
                    webdav_target_checksum,
                    webdav_uploaded_payload_hash,
                    webdav_uploaded_at,
                    webdav_last_attempt_at,
                    webdav_last_error,
                    created_at,
                    updated_at
                FROM shayan_manifest_entries
                WHERE entry_key = ?
                LIMIT 1
                """,
                (str(entry_key),),
            ).fetchone()
        if not row:
            return None
        payload = dict(row)
        try:
            parsed_payload = json.loads(str(payload.pop("payload_json") or "{}"))
        except Exception:
            parsed_payload = {}
        payload["payload"] = parsed_payload if isinstance(parsed_payload, dict) else {}
        return payload


    def delete_shayan_manifest_entry(self, entry_key: str) -> bool:
        """Delete one Shayan manifest entry by key."""
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    DELETE FROM shayan_manifest_entries
                    WHERE entry_key = ?
                    """,
                    (str(entry_key),),
                )
        return int(cur.rowcount or 0) > 0


    def list_shayan_catalog_rows(self) -> List[Dict[str, Any]]:
        """Return merged latest-snapshot + manifest rows for Shayan catalog views."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH latest_snapshot AS (
                    SELECT snapshot_id
                    FROM shayan_snapshots
                    ORDER BY COALESCE(generated_at, created_at) DESC, snapshot_id DESC
                    LIMIT 1
                )
                SELECT
                    COALESCE(s.entry_key, m.entry_key) AS entry_key,
                    s.payload_json AS snapshot_payload_json,
                    m.payload_json AS manifest_payload_json,
                    m.payload_hash AS manifest_payload_hash,
                    m.yadisk_status,
                    m.yadisk_remote_path,
                    m.yadisk_uploaded_payload_hash,
                    m.yadisk_uploaded_at,
                    m.yadisk_last_attempt_at,
                    m.yadisk_last_error,
                    m.webdav_status,
                    m.webdav_remote_path,
                    m.webdav_source_md5,
                    m.webdav_source_size,
                    m.webdav_target_etag,
                    m.webdav_target_checksum,
                    m.webdav_uploaded_payload_hash,
                    m.webdav_uploaded_at,
                    m.webdav_last_attempt_at,
                    m.webdav_last_error,
                    m.updated_at AS manifest_updated_at
                FROM shayan_snapshot_entries s
                FULL OUTER JOIN shayan_manifest_entries m
                  ON m.entry_key = s.entry_key
                WHERE s.snapshot_id = (SELECT snapshot_id FROM latest_snapshot)
                   OR s.snapshot_id IS NULL
                ORDER BY COALESCE(s.entry_key, m.entry_key) ASC
                """
            ).fetchall()

        result: List[Dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            snapshot_raw = str(payload.pop("snapshot_payload_json") or "")
            manifest_raw = str(payload.pop("manifest_payload_json") or "")
            try:
                snapshot_payload = json.loads(snapshot_raw) if snapshot_raw else {}
            except Exception:
                snapshot_payload = {}
            try:
                manifest_payload = json.loads(manifest_raw) if manifest_raw else {}
            except Exception:
                manifest_payload = {}
            payload["snapshot_payload"] = (
                snapshot_payload if isinstance(snapshot_payload, dict) else {}
            )
            payload["manifest_payload"] = (
                manifest_payload if isinstance(manifest_payload, dict) else {}
            )
            result.append(payload)
        return result


    def shayan_manifest_entry_count(self) -> int:
        """Return number of persisted Shayan manifest entries."""
        with self._connect() as conn:
            value = conn.execute(
                "SELECT COUNT(*) AS count FROM shayan_manifest_entries"
            ).scalar()
        return int(value or 0)


    def shayan_manifest_yadisk_uploaded_count(self) -> int:
        """Return count of manifest entries uploaded to Yandex Disk."""
        with self._connect() as conn:
            value = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM shayan_manifest_entries
                WHERE yadisk_status = 'uploaded'
                  AND COALESCE(yadisk_uploaded_payload_hash, '') = COALESCE(payload_hash, '')
                """
            ).scalar()
        return int(value or 0)


    def shayan_manifest_webdav_uploaded_count(self) -> int:
        """Return count of manifest entries uploaded directly to Hetzner WebDAV."""
        with self._connect() as conn:
            value = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM shayan_manifest_entries
                WHERE webdav_status = 'uploaded'
                  AND COALESCE(webdav_uploaded_payload_hash, '') = COALESCE(payload_hash, '')
                  AND COALESCE(webdav_remote_path, '') <> ''
                """
            ).scalar()
        return int(value or 0)


    def replace_shayan_manifest_entries(self, entries: Dict[str, Any]) -> int:
        """Replace Shayan manifest entries with the provided payload map."""
        normalized = _normalize_shayan_entries(entries)
        keys = list(normalized.keys())
        now = utc_now()

        with self._lock:
            with self._connect() as conn:
                if keys:
                    placeholders = ", ".join("?" for _ in keys)
                    conn.execute(
                        f"""
                        DELETE FROM shayan_manifest_entries
                        WHERE entry_key NOT IN ({placeholders})
                        """,
                        keys,
                    )
                else:
                    conn.execute("DELETE FROM shayan_manifest_entries")

                for entry_key in keys:
                    payload = normalized[entry_key]
                    conn.execute(
                        """
                        INSERT INTO shayan_manifest_entries (
                            entry_key,
                            payload_json,
                            payload_hash,
                            created_at,
                            updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(entry_key) DO UPDATE SET
                            payload_json=excluded.payload_json,
                            payload_hash=excluded.payload_hash,
                            updated_at=excluded.updated_at
                        """,
                        (
                            entry_key,
                            json.dumps(payload, ensure_ascii=False),
                            _json_hash(payload),
                            now,
                            now,
                        ),
                    )
        return len(keys)


    def list_shayan_manifest_upload_candidates(
        self,
        *,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Return manifest rows that require Yandex Disk upload/re-upload."""
        row_limit = max(1, min(int(limit), 5000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    entry_key,
                    payload_json,
                    payload_hash,
                    yadisk_status,
                    yadisk_uploaded_payload_hash,
                    yadisk_remote_path,
                    yadisk_uploaded_at,
                    yadisk_last_attempt_at,
                    yadisk_last_error
                FROM shayan_manifest_entries
                WHERE NOT (
                    COALESCE(yadisk_status, 'pending') = 'uploaded'
                    AND COALESCE(yadisk_uploaded_payload_hash, '') = COALESCE(payload_hash, '')
                    AND COALESCE(yadisk_remote_path, '') <> ''
                )
                ORDER BY updated_at ASC, entry_key ASC
                LIMIT ?
                """,
                (row_limit,),
            ).fetchall()

        result: List[Dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            try:
                parsed = json.loads(str(payload.pop("payload_json") or "{}"))
            except Exception:
                parsed = {}
            payload["payload"] = parsed if isinstance(parsed, dict) else {}
            result.append(payload)
        return result


    def mark_shayan_manifest_yadisk_uploaded(
        self,
        entry_key: str,
        *,
        remote_path: str,
        payload_hash: str,
    ) -> int:
        """Mark one manifest entry as uploaded to Yandex Disk."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE shayan_manifest_entries
                    SET
                        yadisk_status = 'uploaded',
                        yadisk_remote_path = ?,
                        yadisk_uploaded_payload_hash = ?,
                        yadisk_uploaded_at = ?,
                        yadisk_last_attempt_at = ?,
                        yadisk_last_error = NULL,
                        updated_at = ?
                    WHERE entry_key = ?
                    """,
                    (
                        str(remote_path or "").strip() or None,
                        str(payload_hash or "").strip() or None,
                        now,
                        now,
                        now,
                        str(entry_key),
                    ),
                )
        return int(cur.rowcount)


    def mark_shayan_manifest_yadisk_failed(
        self,
        entry_key: str,
        *,
        error_text: str,
    ) -> int:
        """Mark one manifest entry Yandex Disk upload as failed."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE shayan_manifest_entries
                    SET
                        yadisk_status = 'failed',
                        yadisk_last_attempt_at = ?,
                        yadisk_last_error = ?,
                        updated_at = ?
                    WHERE entry_key = ?
                    """,
                    (
                        now,
                        str(error_text or "").strip()[:4000],
                        now,
                        str(entry_key),
                    ),
                )
        return int(cur.rowcount)


    def list_shayan_manifest_webdav_upload_candidates(
        self,
        *,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Return manifest rows requiring direct upload to Hetzner WebDAV."""
        row_limit = max(1, min(int(limit), 5000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    entry_key,
                    payload_json,
                    payload_hash,
                    webdav_status,
                    webdav_remote_path AS target_path,
                    webdav_source_md5 AS source_md5,
                    webdav_source_size AS source_size,
                    webdav_target_etag AS target_etag,
                    webdav_target_checksum AS target_checksum,
                    webdav_uploaded_payload_hash,
                    webdav_uploaded_at,
                    webdav_last_attempt_at,
                    webdav_last_error
                FROM shayan_manifest_entries
                WHERE NOT (
                    COALESCE(webdav_status, 'pending') = 'legacy_yadisk'
                    AND COALESCE(yadisk_uploaded_payload_hash, '') = COALESCE(payload_hash, '')
                )
                  AND NOT (
                    COALESCE(webdav_status, 'pending') = 'uploaded'
                    AND COALESCE(webdav_uploaded_payload_hash, '') = COALESCE(payload_hash, '')
                    AND COALESCE(webdav_remote_path, '') <> ''
                )
                ORDER BY updated_at ASC, entry_key ASC
                LIMIT ?
                """,
                (row_limit,),
            ).fetchall()

        result: List[Dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            try:
                parsed = json.loads(str(payload.pop("payload_json") or "{}"))
            except Exception:
                parsed = {}
            payload["payload"] = parsed if isinstance(parsed, dict) else {}
            result.append(payload)
        return result


    def mark_shayan_manifest_webdav_upload_started(
        self,
        entry_key: str,
        *,
        remote_path: str,
        source_md5: str,
        source_size: int,
        payload_hash: str,
    ) -> int:
        """Persist local identity before starting an external WebDAV mutation."""
        _ = payload_hash
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE shayan_manifest_entries
                    SET
                        webdav_status = 'uploading',
                        webdav_remote_path = ?,
                        webdav_source_md5 = ?,
                        webdav_source_size = ?,
                        webdav_target_etag = NULL,
                        webdav_target_checksum = NULL,
                        webdav_last_attempt_at = ?,
                        webdav_last_error = NULL,
                        updated_at = ?
                    WHERE entry_key = ?
                    """,
                    (
                        str(remote_path),
                        str(source_md5).lower(),
                        int(source_size),
                        now,
                        now,
                        str(entry_key),
                    ),
                )
        return int(cur.rowcount or 0)


    def mark_shayan_manifest_webdav_uploaded(
        self,
        entry_key: str,
        *,
        remote_path: str,
        payload_hash: str,
        target_etag: str,
        target_checksum: str,
    ) -> int:
        """Mark one manifest entry as independently verified on Hetzner WebDAV."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE shayan_manifest_entries
                    SET
                        webdav_status = 'uploaded',
                        webdav_remote_path = ?,
                        webdav_target_etag = ?,
                        webdav_target_checksum = ?,
                        webdav_uploaded_payload_hash = ?,
                        webdav_uploaded_at = ?,
                        webdav_last_attempt_at = ?,
                        webdav_last_error = NULL,
                        updated_at = ?
                    WHERE entry_key = ?
                    """,
                    (
                        str(remote_path),
                        str(target_etag or "").strip() or None,
                        str(target_checksum).strip().lower(),
                        str(payload_hash),
                        now,
                        now,
                        now,
                        str(entry_key),
                    ),
                )
        return int(cur.rowcount or 0)


    def mark_shayan_manifest_webdav_failed(
        self,
        entry_key: str,
        *,
        error_text: str,
    ) -> int:
        """Persist an actionable direct-to-WebDAV upload failure."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE shayan_manifest_entries
                    SET
                        webdav_status = 'failed',
                        webdav_last_attempt_at = ?,
                        webdav_last_error = ?,
                        updated_at = ?
                    WHERE entry_key = ?
                    """,
                    (
                        now,
                        str(error_text or "").strip()[:4000],
                        now,
                        str(entry_key),
                    ),
                )
        return int(cur.rowcount or 0)


    def get_latest_shayan_snapshot(self) -> Optional[Dict[str, Any]]:
        """Return latest Shayan snapshot header row."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    snapshot_id,
                    run_id,
                    source,
                    generated_at,
                    entries_count,
                    created_at,
                    updated_at
                FROM shayan_snapshots
                ORDER BY COALESCE(generated_at, created_at) DESC, snapshot_id DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None


    def get_latest_shayan_snapshot_entry_hashes(self) -> Dict[str, str]:
        """Return entry hash map for the most recent Shayan snapshot."""
        entries = self.get_latest_shayan_snapshot_entries()
        return {key: _json_hash(value) for key, value in entries.items()}


    def get_latest_shayan_snapshot_entries(self) -> Dict[str, Dict[str, Any]]:
        """Return full entry payload map for the most recent Shayan snapshot."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT e.entry_key, e.payload_json
                FROM shayan_snapshot_entries e
                JOIN shayan_snapshots s
                  ON s.snapshot_id = e.snapshot_id
                WHERE s.snapshot_id = (
                    SELECT snapshot_id
                    FROM shayan_snapshots
                    ORDER BY COALESCE(generated_at, created_at) DESC, snapshot_id DESC
                    LIMIT 1
                )
                ORDER BY e.entry_key ASC
                """
            ).fetchall()
        payload: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            entry_key = str(row.get("entry_key") or "").strip()
            if not entry_key:
                continue
            try:
                value = json.loads(str(row.get("payload_json") or "{}"))
            except Exception:
                value = {}
            payload[entry_key] = value if isinstance(value, dict) else {}
        return payload


    def create_shayan_snapshot(
        self,
        entries: Dict[str, Any],
        *,
        run_id: Optional[int] = None,
        source: Optional[str] = None,
        generated_at: Optional[str] = None,
    ) -> int:
        """Persist one full Shayan snapshot with versioned entries."""
        normalized = _normalize_shayan_entries(entries)
        now = utc_now()
        created_snapshot_id = 0

        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO shayan_snapshots (
                        run_id,
                        source,
                        generated_at,
                        entries_count,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    RETURNING snapshot_id
                    """,
                    (
                        run_id,
                        source,
                        generated_at,
                        len(normalized),
                        now,
                        now,
                    ),
                ).fetchone()
                created_snapshot_id = int((row or {}).get("snapshot_id") or 0)
                if created_snapshot_id <= 0:
                    raise RuntimeError("Failed to create Shayan snapshot row")

                for entry_key, payload in normalized.items():
                    conn.execute(
                        """
                        INSERT INTO shayan_snapshot_entries (
                            snapshot_id,
                            entry_key,
                            payload_json,
                            payload_hash,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            created_snapshot_id,
                            entry_key,
                            json.dumps(payload, ensure_ascii=False),
                            _json_hash(payload),
                            now,
                        ),
                    )
        return created_snapshot_id


    def replace_shayan_run_changes(
        self,
        run_id: int,
        changes: List[Dict[str, Any]],
    ) -> int:
        """Replace detailed Shayan changes for one run."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM shayan_run_changes WHERE run_id = ?", (run_id,))
                for item in changes:
                    change_type = str(item.get("change_type") or "").strip().lower()
                    if change_type not in {"added", "changed", "removed"}:
                        continue
                    entry_key = str(item.get("entry_key") or "").strip()
                    if not entry_key:
                        continue
                    season = item.get("season")
                    episode = item.get("episode")
                    conn.execute(
                        """
                        INSERT INTO shayan_run_changes (
                            run_id,
                            change_type,
                            entry_key,
                            category,
                            program,
                            season,
                            episode,
                            title,
                            old_payload_json,
                            new_payload_json,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(run_id),
                            change_type,
                            entry_key,
                            str(item.get("category") or "").strip() or None,
                            str(item.get("program") or "").strip() or None,
                            int(season) if season is not None else None,
                            int(episode) if episode is not None else None,
                            str(item.get("title") or "").strip() or None,
                            json.dumps(item.get("old_payload") or {}, ensure_ascii=False),
                            json.dumps(item.get("new_payload") or {}, ensure_ascii=False),
                            now,
                        ),
                    )
        return len(changes)


    def list_shayan_run_changes(
        self,
        run_id: int,
        *,
        change_type: Optional[str] = None,
        after_change_id: int = 0,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return detailed Shayan change rows for one run."""
        row_limit = max(1, min(int(limit), 500))
        params: List[Any] = [int(run_id), int(after_change_id)]
        where = "WHERE run_id = ? AND change_id > ?"
        normalized = str(change_type or "").strip().lower()
        if normalized:
            where += " AND change_type = ?"
            params.append(normalized)
        params.append(row_limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    change_id,
                    run_id,
                    change_type,
                    entry_key,
                    category,
                    program,
                    season,
                    episode,
                    title,
                    old_payload_json,
                    new_payload_json,
                    created_at
                FROM shayan_run_changes
                {where}
                ORDER BY change_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()

        result: List[Dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            try:
                payload["old_payload"] = json.loads(str(payload.pop("old_payload_json") or "{}"))
            except Exception:
                payload["old_payload"] = {}
            try:
                payload["new_payload"] = json.loads(str(payload.pop("new_payload_json") or "{}"))
            except Exception:
                payload["new_payload"] = {}
            result.append(payload)
        return result


    def count_shayan_run_changes(
        self,
        run_id: int,
    ) -> Dict[str, int]:
        """Return detailed Shayan change counters grouped by type for one run."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT change_type, COUNT(*) AS count
                FROM shayan_run_changes
                WHERE run_id = ?
                GROUP BY change_type
                """,
                (run_id,),
            ).fetchall()
        counts = {str(row.get("change_type") or ""): int(row.get("count") or 0) for row in rows}
        return {
            "added": int(counts.get("added") or 0),
            "changed": int(counts.get("changed") or 0),
            "removed": int(counts.get("removed") or 0),
            "total": int(sum(counts.values())),
        }
