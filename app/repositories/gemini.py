from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.repositories.core import utc_now


class GeminiRepository:
    """Machine-local SQLite operations for Gemini coordination."""

    def upsert_gemini_keys(self, keys: List[Dict[str, Any]]) -> None:
        """Synchronize configured Gemini keys into runtime registry."""
        now = utc_now()
        normalized = [
            {
                "key_id": str(item.get("key_id") or ""),
                "account_id": str(item.get("account_id") or "default"),
                "masked_key": str(item.get("masked_key") or ""),
                "quota_domain_id": str(
                    item.get("quota_domain_id") or item.get("key_id") or ""
                ),
            }
            for item in keys
        ]
        with self._lock:
            with self._runtime_connect(immediate=True) as conn:
                key_ids = [item["key_id"] for item in normalized]
                if key_ids:
                    placeholders = ", ".join("?" for _ in key_ids)
                    conn.execute(
                        f"UPDATE gemini_keys SET active=0, updated_at=? "
                        f"WHERE key_id NOT IN ({placeholders})",
                        (now, *key_ids),
                    )
                else:
                    conn.execute("UPDATE gemini_keys SET active=0, updated_at=?", (now,))
                for item in normalized:
                    conn.execute(
                        """INSERT INTO gemini_keys (
                               key_id, account_id, masked_key, quota_domain_id,
                               active, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, 1, ?, ?)
                           ON CONFLICT(key_id) DO UPDATE SET
                               account_id=excluded.account_id,
                               masked_key=excluded.masked_key,
                               quota_domain_id=excluded.quota_domain_id,
                               active=1, updated_at=excluded.updated_at""",
                        (
                            item["key_id"],
                            item["account_id"],
                            item["masked_key"],
                            item["quota_domain_id"],
                            now,
                            now,
                        ),
                    )
                    conn.execute(
                        """INSERT INTO gemini_account_leases (
                               account_id, created_at, updated_at
                           ) VALUES (?, ?, ?)
                           ON CONFLICT(account_id) DO NOTHING""",
                        (item["account_id"], now, now),
                    )

    def try_claim_gemini_request_slot(
        self,
        *,
        model_name: str,
        task_id: Optional[str],
        run_id: Optional[int],
        now_ts: str,
        window_start_ts: str,
        max_requests: int,
    ) -> Dict[str, Any]:
        """Atomically reserve one slot in the shared sliding request window."""
        with self._runtime_connect(immediate=True) as conn:
            conn.execute(
                "DELETE FROM gemini_request_slots WHERE requested_at <= ?",
                (window_start_ts,),
            )
            row = conn.execute(
                "SELECT COUNT(*) AS request_count, MIN(requested_at) AS oldest_request_at "
                "FROM gemini_request_slots"
            ).fetchone() or {}
            request_count = int(row.get("request_count") or 0)
            if request_count >= max_requests:
                return {
                    "claimed": False,
                    "oldest_request_at": row.get("oldest_request_at"),
                    "requests_in_window": request_count,
                }
            conn.execute(
                """INSERT INTO gemini_request_slots (
                       model_name, task_id, run_id, requested_at
                   ) VALUES (?, ?, ?, ?)""",
                (model_name, task_id, run_id, now_ts),
            )
            return {
                "claimed": True,
                "oldest_request_at": row.get("oldest_request_at"),
                "requests_in_window": request_count + 1,
            }

    def record_gemini_generic_quota_signal(
        self,
        *,
        model_name: str,
        quota_domain_id: str,
        now_ts: str,
        window_start_ts: str,
    ) -> int:
        """Record one generic 429 and count distinct recent quota domains."""
        with self._runtime_connect(immediate=True) as conn:
            conn.execute(
                "DELETE FROM gemini_generic_quota_signals "
                "WHERE model_name = ? AND last_seen_at <= ?",
                (model_name, window_start_ts),
            )
            conn.execute(
                """INSERT INTO gemini_generic_quota_signals (
                       model_name, quota_domain_id, last_seen_at
                   ) VALUES (?, ?, ?)
                   ON CONFLICT(model_name, quota_domain_id) DO UPDATE SET
                       last_seen_at=excluded.last_seen_at""",
                (model_name, quota_domain_id, now_ts),
            )
            row = conn.execute(
                "SELECT COUNT(*) AS domain_count "
                "FROM gemini_generic_quota_signals WHERE model_name = ?",
                (model_name,),
            ).fetchone() or {}
        return int(row.get("domain_count") or 0)

    def clear_gemini_generic_quota_signals(self, model_name: str) -> int:
        """Close the generic-429 circuit history after a successful request."""
        with self._runtime_connect(immediate=True) as conn:
            cur = conn.execute(
                "DELETE FROM gemini_generic_quota_signals WHERE model_name = ?",
                (model_name,),
            )
        return int(cur.rowcount or 0)

    def ensure_gemini_runtime_cycle(self, cycle_label: str) -> Dict[str, Any]:
        """Read the current cycle cheaply and reset it atomically when needed."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect(immediate=True) as conn:
                row = conn.execute(
                    """
                    SELECT control_id, cycle_label, pause_until, last_pause_reason,
                           blackout_override_until, updated_at
                    FROM gemini_runtime_control
                    WHERE control_id = 1
                    FOR UPDATE
                    """
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        """
                        INSERT INTO gemini_runtime_control (
                            control_id, cycle_label, pause_until, last_pause_reason,
                            blackout_override_until, updated_at
                        ) VALUES (1, ?, NULL, NULL, NULL, ?)
                        RETURNING control_id, cycle_label, pause_until,
                                  last_pause_reason, blackout_override_until,
                                  updated_at
                        """,
                        (cycle_label, now),
                    ).fetchone()
                    return {**dict(row or {}), "rolled": False}
                if str(row.get("cycle_label") or "") == cycle_label:
                    return {**dict(row), "rolled": False}

                row = conn.execute(
                    """
                    UPDATE gemini_runtime_control
                    SET cycle_label = ?, pause_until = NULL,
                        blackout_override_until = NULL, updated_at = ?
                    WHERE control_id = 1
                    RETURNING control_id, cycle_label, pause_until,
                              last_pause_reason, blackout_override_until,
                              updated_at
                    """,
                    (cycle_label, now),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE gemini_key_model_state
                        SET exhausted = 0,
                            exhausted_at = NULL,
                            cooldown_until = NULL,
                            attempts_cycle = 0,
                            success_cycle = 0,
                            updated_at = ?
                    """,
                    (now,),
                )
                conn.execute("DELETE FROM gemini_quota_domain_model_state")
        return {**dict(row), "rolled": True} if row else {
            "control_id": 1,
            "cycle_label": cycle_label,
            "pause_until": None,
            "last_pause_reason": None,
            "blackout_override_until": None,
            "updated_at": now,
            "rolled": True,
        }

    def list_gemini_account_leases(self) -> List[Dict[str, Any]]:
        """List account leases in least-recently-used order."""
        with self._runtime_connect() as conn:
            rows = conn.execute(
                """SELECT account_id, lease_token, task_id, run_id, worker_id,
                          lease_expires_at, last_acquired_at, created_at, updated_at
                   FROM gemini_account_leases
                   ORDER BY last_acquired_at NULLS FIRST, account_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_gemini_quota_domain_model_state(
        self, quota_domain_id: str, model_name: str
    ) -> Optional[Dict[str, Any]]:
        with self._runtime_connect() as conn:
            row = conn.execute(
                """SELECT quota_domain_id, model_name, cooldown_until, failure_count,
                          last_error_at, last_error_text, created_at, updated_at
                   FROM gemini_quota_domain_model_state
                   WHERE quota_domain_id = ? AND model_name = ?""",
                (quota_domain_id, model_name),
            ).fetchone()
        return dict(row) if row else None

    def list_gemini_quota_domain_model_states(
        self, *, model_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        params: tuple[Any, ...] = ()
        where = ""
        if model_name:
            where = "WHERE model_name = ?"
            params = (model_name,)
        with self._runtime_connect() as conn:
            rows = conn.execute(
                f"""SELECT quota_domain_id, model_name, cooldown_until, failure_count,
                           last_error_at, last_error_text, created_at, updated_at
                    FROM gemini_quota_domain_model_state
                    {where}
                    ORDER BY quota_domain_id, model_name""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_gemini_quota_domain_model_cooldown(
        self,
        quota_domain_id: str,
        model_name: str,
        *,
        cooldown_until: str,
        failure_count: int,
        now_ts: str,
        error_text: str,
    ) -> None:
        """Persist a temporary project/account quota circuit breaker."""
        with self._runtime_connect(immediate=True) as conn:
            conn.execute(
                """INSERT INTO gemini_quota_domain_model_state (
                       quota_domain_id, model_name, cooldown_until, failure_count,
                       last_error_at, last_error_text, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(quota_domain_id, model_name) DO UPDATE SET
                       cooldown_until=excluded.cooldown_until,
                       failure_count=excluded.failure_count,
                       last_error_at=excluded.last_error_at,
                       last_error_text=excluded.last_error_text,
                       updated_at=excluded.updated_at""",
                (
                    quota_domain_id,
                    model_name,
                    cooldown_until,
                    failure_count,
                    now_ts,
                    error_text,
                    now_ts,
                    now_ts,
                ),
            )

    def clear_gemini_quota_domain_model_state(
        self, quota_domain_id: str, model_name: str
    ) -> int:
        with self._runtime_connect(immediate=True) as conn:
            cur = conn.execute(
                "DELETE FROM gemini_quota_domain_model_state "
                "WHERE quota_domain_id = ? AND model_name = ?",
                (quota_domain_id, model_name),
            )
        return int(cur.rowcount or 0)

    def mark_gemini_quota_domain_model_exhausted(
        self,
        quota_domain_id: str,
        model_name: str,
        *,
        now_ts: str,
        error_text: str,
    ) -> int:
        """Mark all keys in one quota domain exhausted after explicit daily evidence."""
        with self._runtime_connect(immediate=True) as conn:
            cur = conn.execute(
                """UPDATE gemini_key_model_state
                   SET exhausted = 1, exhausted_at = ?, last_error_at = ?,
                       last_error_text = ?, updated_at = ?
                   WHERE model_name = ? AND key_id IN (
                       SELECT key_id FROM gemini_keys
                       WHERE quota_domain_id = ? AND active = 1
                   )""",
                (now_ts, now_ts, error_text, now_ts, model_name, quota_domain_id),
            )
            conn.execute(
                "DELETE FROM gemini_quota_domain_model_state "
                "WHERE quota_domain_id = ? AND model_name = ?",
                (quota_domain_id, model_name),
            )
        return int(cur.rowcount or 0)

    def get_gemini_snapshot_metadata(self) -> Dict[str, List[Dict[str, Any]]]:
        """Read account leases and model runtime through one pool checkout."""
        with self._runtime_connect() as conn:
            account_leases = conn.execute(
                """SELECT account_id, lease_token, task_id, run_id, worker_id,
                          lease_expires_at, last_acquired_at, created_at, updated_at
                   FROM gemini_account_leases
                   ORDER BY last_acquired_at NULLS FIRST, account_id"""
            ).fetchall()
            model_runtime = conn.execute(
                """SELECT model_name, pause_until, last_pause_reason, created_at, updated_at
                   FROM gemini_model_runtime ORDER BY model_name"""
            ).fetchall()
            quota_domain_model_states = conn.execute(
                """SELECT quota_domain_id, model_name, cooldown_until, failure_count,
                          last_error_at, created_at, updated_at
                   FROM gemini_quota_domain_model_state
                   ORDER BY quota_domain_id, model_name"""
            ).fetchall()
        return {
            "account_leases": [dict(row) for row in account_leases],
            "model_runtime": [dict(row) for row in model_runtime],
            "quota_domain_model_states": [
                dict(row) for row in quota_domain_model_states
            ],
        }

    def try_claim_gemini_account(
        self,
        account_id: str,
        *,
        lease_token: str,
        task_id: Optional[str],
        run_id: Optional[int],
        worker_id: str,
        now_ts: str,
        expires_at: str,
    ) -> bool:
        """Atomically claim an idle, expired, or orphaned account lease."""
        with self._runtime_connect(immediate=True) as conn:
            cur = conn.execute(
                """
                UPDATE gemini_account_leases
                SET lease_token = ?, task_id = ?, run_id = ?, worker_id = ?,
                    lease_expires_at = ?, last_acquired_at = ?, updated_at = ?
                WHERE account_id = ?
                  AND (
                    lease_token IS NULL
                    OR lease_expires_at IS NULL
                    OR lease_expires_at <= ?
                    OR (run_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM runs
                        WHERE runs.run_id = gemini_account_leases.run_id
                          AND runs.status IN (
                            'starting', 'running', 'stopping_graceful', 'stopping_force'
                          )
                    ))
                  )
                """,
                (
                    lease_token, task_id, run_id, worker_id, expires_at,
                    now_ts, now_ts, account_id, now_ts,
                ),
            )
            return int(cur.rowcount or 0) > 0

    def renew_gemini_account_lease(
        self, account_id: str, lease_token: str, *, expires_at: str, now_ts: str
    ) -> bool:
        """Extend a lease only while its ownership token still matches."""
        with self._runtime_connect() as conn:
            cur = conn.execute(
                """UPDATE gemini_account_leases
                   SET lease_expires_at = ?, updated_at = ?
                   WHERE account_id = ? AND lease_token = ?""",
                (expires_at, now_ts, account_id, lease_token),
            )
            return int(cur.rowcount or 0) > 0

    def release_gemini_account_lease(self, account_id: str, lease_token: str) -> bool:
        """Release an account lease without disturbing a newer owner."""
        now = utc_now()
        with self._runtime_connect() as conn:
            cur = conn.execute(
                """UPDATE gemini_account_leases
                   SET lease_token = NULL, task_id = NULL, run_id = NULL,
                       worker_id = NULL, lease_expires_at = NULL, updated_at = ?
                   WHERE account_id = ? AND lease_token = ?""",
                (now, account_id, lease_token),
            )
            return int(cur.rowcount or 0) > 0

    def ensure_gemini_model_runtime(self, model_name: str) -> None:
        now = utc_now()
        with self._runtime_connect() as conn:
            conn.execute(
                """INSERT INTO gemini_model_runtime (
                       model_name, pause_until, last_pause_reason, created_at, updated_at
                   ) VALUES (?, NULL, NULL, ?, ?)
                   ON CONFLICT(model_name) DO NOTHING""",
                (model_name, now, now),
            )

    def list_gemini_model_runtime(self) -> List[Dict[str, Any]]:
        with self._runtime_connect() as conn:
            rows = conn.execute(
                """SELECT model_name, pause_until, last_pause_reason, created_at, updated_at
                   FROM gemini_model_runtime ORDER BY model_name"""
            ).fetchall()
        return [dict(row) for row in rows]

    def set_gemini_model_pause(
        self, model_name: str, pause_until: Optional[str], reason: Optional[str]
    ) -> Dict[str, Any]:
        now = utc_now()
        with self._runtime_connect() as conn:
            row = conn.execute(
                """INSERT INTO gemini_model_runtime (
                       model_name, pause_until, last_pause_reason, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(model_name) DO UPDATE SET
                       pause_until=excluded.pause_until,
                       last_pause_reason=excluded.last_pause_reason,
                       updated_at=excluded.updated_at
                   RETURNING model_name, pause_until, last_pause_reason, created_at, updated_at""",
                (model_name, pause_until, reason, now, now),
            ).fetchone()
        return dict(row) if row else {}


    def ensure_gemini_model_state(self, key_id: str, model_name: str) -> None:
        """Ensure one key+model runtime row exists."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect(immediate=True) as conn:
                conn.execute(
                    """
                    INSERT INTO gemini_key_model_state (
                        key_id, model_name, exhausted, updated_at
                    ) VALUES (?, ?, 0, ?)
                    ON CONFLICT(key_id, model_name) DO NOTHING
                    """,
                    (key_id, model_name, now),
                )

    def ensure_gemini_model_states(
        self, key_ids: List[str], model_name: str
    ) -> None:
        """Bulk-initialize one model for all configured keys in one checkout."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect(immediate=True) as conn:
                conn.execute(
                    """INSERT INTO gemini_model_runtime (
                           model_name, pause_until, last_pause_reason, created_at, updated_at
                       ) VALUES (?, NULL, NULL, ?, ?)
                       ON CONFLICT(model_name) DO NOTHING""",
                    (model_name, now, now),
                )
                for key_id in key_ids:
                    conn.execute(
                        """INSERT INTO gemini_key_model_state (
                               key_id, model_name, exhausted, updated_at
                           ) VALUES (?, ?, 0, ?)
                           ON CONFLICT(key_id, model_name) DO NOTHING""",
                        (str(key_id), model_name, now),
                    )


    def list_gemini_keys(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        """List Gemini key registry rows."""
        where = "WHERE active = 1" if active_only else ""
        with self._runtime_connect() as conn:
            rows = conn.execute(
                f"""
                SELECT key_id, account_id, masked_key, active, created_at, updated_at
                FROM gemini_keys
                {where}
                ORDER BY account_id ASC, key_id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]


    def list_gemini_model_states(self, *, model_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """List Gemini key-model runtime rows joined with key metadata."""
        params: List[Any] = []
        where = "WHERE k.active = 1"
        if model_name:
            where += " AND s.model_name = ?"
            params.append(model_name)
        with self._runtime_connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    k.key_id,
                    k.account_id,
                    k.masked_key,
                    s.model_name,
                    s.exhausted,
                    s.exhausted_at,
                    s.cooldown_until,
                    s.last_used_at,
                    s.last_success_at,
                    s.last_error_at,
                    s.last_error_text,
                    s.attempts_total,
                    s.attempts_cycle,
                    s.success_total,
                    s.success_cycle,
                    s.updated_at
                FROM gemini_keys k
                LEFT JOIN gemini_key_model_state s
                    ON s.key_id = k.key_id
                {where}
                ORDER BY k.account_id ASC, k.key_id ASC, s.model_name ASC
                """,
                params,
            ).fetchall()
        payload = [dict(row) for row in rows]
        for item in payload:
            item["exhausted"] = bool(item.get("exhausted", 0))
        return payload


    def ensure_gemini_runtime_control(self, cycle_label: str) -> Dict[str, Any]:
        """Ensure one global Gemini runtime control row exists."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO gemini_runtime_control (
                        control_id, cycle_label, pause_until, last_pause_reason,
                        blackout_override_until, updated_at
                    ) VALUES (1, ?, NULL, NULL, NULL, ?)
                    ON CONFLICT(control_id) DO NOTHING
                    """,
                    (cycle_label, now),
                )
                row = conn.execute(
                    """
                    SELECT control_id, cycle_label, pause_until, last_pause_reason,
                           blackout_override_until, updated_at
                    FROM gemini_runtime_control
                    WHERE control_id = 1
                    """
                ).fetchone()
        return dict(row) if row else {
            "control_id": 1,
            "cycle_label": cycle_label,
            "pause_until": None,
            "last_pause_reason": None,
            "blackout_override_until": None,
            "updated_at": now,
        }


    def rollover_gemini_cycle(self, cycle_label: str) -> bool:
        """Reset exhausted/cycle counters when Gemini day cycle changes."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect() as conn:
                row = conn.execute(
                    """
                    SELECT cycle_label
                    FROM gemini_runtime_control
                    WHERE control_id = 1
                    """
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO gemini_runtime_control (
                            control_id, cycle_label, pause_until, last_pause_reason,
                            blackout_override_until, updated_at
                        ) VALUES (1, ?, NULL, NULL, NULL, ?)
                        """,
                        (cycle_label, now),
                    )
                    return False

                if str(row.get("cycle_label") or "") == cycle_label:
                    return False

                conn.execute(
                    """
                    UPDATE gemini_runtime_control
                    SET cycle_label = ?, pause_until = NULL,
                        blackout_override_until = NULL, updated_at = ?
                    WHERE control_id = 1
                    """,
                    (cycle_label, now),
                )
                conn.execute(
                    """
                    UPDATE gemini_key_model_state
                    SET exhausted = 0,
                        exhausted_at = NULL,
                        cooldown_until = NULL,
                        attempts_cycle = 0,
                        success_cycle = 0,
                        updated_at = ?
                    """,
                    (now,),
                )
                conn.execute("DELETE FROM gemini_quota_domain_model_state")
                return True


    def set_gemini_pause(self, pause_until: Optional[str], reason: Optional[str] = None) -> Dict[str, Any]:
        """Set or clear global Gemini pause timestamp."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect() as conn:
                conn.execute(
                    """
                    UPDATE gemini_runtime_control
                    SET pause_until = ?, last_pause_reason = ?, updated_at = ?
                    WHERE control_id = 1
                    """,
                    (pause_until, reason, now),
                )
                row = conn.execute(
                    """
                    SELECT control_id, cycle_label, pause_until, last_pause_reason,
                           blackout_override_until, updated_at
                    FROM gemini_runtime_control
                    WHERE control_id = 1
                    """
                ).fetchone()
        return dict(row) if row else {
            "control_id": 1,
            "cycle_label": "",
            "pause_until": pause_until,
            "last_pause_reason": reason,
            "blackout_override_until": None,
            "updated_at": now,
        }


    def set_gemini_blackout_override(
        self, override_until: Optional[str]
    ) -> Dict[str, Any]:
        """Set or clear the current global Gemini blackout override."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect() as conn:
                conn.execute(
                    """
                    UPDATE gemini_runtime_control
                    SET blackout_override_until = ?, updated_at = ?
                    WHERE control_id = 1
                    """,
                    (override_until, now),
                )
                row = conn.execute(
                    """
                    SELECT control_id, cycle_label, pause_until, last_pause_reason,
                           blackout_override_until, updated_at
                    FROM gemini_runtime_control
                    WHERE control_id = 1
                    """
                ).fetchone()
        return dict(row) if row else {
            "control_id": 1,
            "cycle_label": "",
            "pause_until": None,
            "last_pause_reason": None,
            "blackout_override_until": override_until,
            "updated_at": now,
        }


    def try_claim_gemini_key_use(
        self,
        key_id: str,
        model_name: str,
        *,
        now_ts: str,
        cooldown_until: str,
    ) -> bool:
        """Atomically reserve one key+model usage slot if ready and not exhausted."""
        with self._lock:
            with self._runtime_connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE gemini_key_model_state
                    SET last_used_at = ?,
                        cooldown_until = ?,
                        attempts_total = attempts_total + 1,
                        attempts_cycle = attempts_cycle + 1,
                        updated_at = ?
                    WHERE key_id = ?
                      AND model_name = ?
                      AND exhausted = 0
                      AND (cooldown_until IS NULL OR cooldown_until <= ?)
                    """,
                    (
                        now_ts,
                        cooldown_until,
                        now_ts,
                        key_id,
                        model_name,
                        now_ts,
                    ),
                )
                return int(cur.rowcount or 0) > 0


    def mark_gemini_success(
        self,
        key_id: str,
        model_name: str,
        *,
        now_ts: str,
    ) -> None:
        """Persist successful Gemini call for one key+model."""
        with self._lock:
            with self._runtime_connect() as conn:
                conn.execute(
                    """
                    UPDATE gemini_key_model_state
                    SET last_success_at = ?,
                        success_total = success_total + 1,
                        success_cycle = success_cycle + 1,
                        last_error_at = NULL,
                        last_error_text = NULL,
                        updated_at = ?
                    WHERE key_id = ? AND model_name = ?
                    """,
                    (now_ts, now_ts, key_id, model_name),
                )


    def mark_gemini_error(
        self,
        key_id: str,
        model_name: str,
        *,
        now_ts: str,
        error_text: str,
        exhausted: bool = False,
    ) -> None:
        """Persist failed Gemini call metadata for one key+model."""
        with self._lock:
            with self._runtime_connect() as conn:
                conn.execute(
                    """
                    UPDATE gemini_key_model_state
                    SET last_error_at = ?,
                        last_error_text = ?,
                        exhausted = CASE WHEN ? THEN 1 ELSE exhausted END,
                        exhausted_at = CASE WHEN ? THEN ? ELSE exhausted_at END,
                        updated_at = ?
                    WHERE key_id = ? AND model_name = ?
                    """,
                    (
                        now_ts,
                        error_text,
                        bool(exhausted),
                        bool(exhausted),
                        now_ts,
                        now_ts,
                        key_id,
                        model_name,
                    ),
                )


    def reset_gemini_key_exhaustion(self, key_id: str) -> int:
        """Clear one key and its quota-domain cooldowns."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE gemini_key_model_state
                    SET exhausted = 0, exhausted_at = NULL, updated_at = ?
                    WHERE key_id = ?
                    """,
                    (now, key_id),
                )
                quota_cur = conn.execute(
                    """DELETE FROM gemini_quota_domain_model_state
                       WHERE quota_domain_id = (
                           SELECT quota_domain_id FROM gemini_keys WHERE key_id = ?
                       )""",
                    (key_id,),
                )
                return int(cur.rowcount or 0) + int(quota_cur.rowcount or 0)


    def reset_all_gemini_exhaustion(self) -> int:
        """Clear daily exhaustion and temporary quota cooldowns."""
        now = utc_now()
        with self._lock:
            with self._runtime_connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE gemini_key_model_state
                    SET exhausted = 0, exhausted_at = NULL, updated_at = ?
                    WHERE exhausted = 1
                    """,
                    (now,),
                )
                quota_cur = conn.execute(
                    "DELETE FROM gemini_quota_domain_model_state"
                )
                return int(cur.rowcount or 0) + int(quota_cur.rowcount or 0)
