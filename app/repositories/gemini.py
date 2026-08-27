from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.repositories.core import utc_now


class GeminiRepository:
    """PostgreSQL operations for the gemini domain."""

    def upsert_gemini_keys(self, keys: List[Dict[str, Any]]) -> None:
        """Synchronize configured Gemini keys into runtime registry."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE gemini_keys SET active = 0, updated_at = ?",
                    (now,),
                )
                for item in keys:
                    conn.execute(
                        """
                        INSERT INTO gemini_keys (
                            key_id, account_id, masked_key, active, created_at, updated_at
                        ) VALUES (?, ?, ?, 1, ?, ?)
                        ON CONFLICT(key_id) DO UPDATE SET
                            account_id=excluded.account_id,
                            masked_key=excluded.masked_key,
                            active=excluded.active,
                            updated_at=excluded.updated_at
                        """,
                        (
                            str(item.get("key_id") or ""),
                            str(item.get("account_id") or "default"),
                            str(item.get("masked_key") or ""),
                            now,
                            now,
                        ),
                    )


    def ensure_gemini_model_state(self, key_id: str, model_name: str) -> None:
        """Ensure one key+model runtime row exists."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO gemini_key_model_state (
                        key_id, model_name, exhausted, updated_at
                    ) VALUES (?, ?, 0, ?)
                    ON CONFLICT(key_id, model_name) DO NOTHING
                    """,
                    (key_id, model_name, now),
                )


    def list_gemini_keys(self, *, active_only: bool = True) -> List[Dict[str, Any]]:
        """List Gemini key registry rows."""
        where = "WHERE active = 1" if active_only else ""
        with self._connect() as conn:
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
        with self._connect() as conn:
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
            with self._connect() as conn:
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
            with self._connect() as conn:
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
                return True


    def set_gemini_pause(self, pause_until: Optional[str], reason: Optional[str] = None) -> Dict[str, Any]:
        """Set or clear global Gemini pause timestamp."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
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
            with self._connect() as conn:
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
            with self._connect() as conn:
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
            with self._connect() as conn:
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
            with self._connect() as conn:
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
        """Clear exhaustion marker for all models of one key."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE gemini_key_model_state
                    SET exhausted = 0, exhausted_at = NULL, updated_at = ?
                    WHERE key_id = ?
                    """,
                    (now, key_id),
                )
                return int(cur.rowcount or 0)


    def reset_all_gemini_exhaustion(self) -> int:
        """Clear exhaustion marker for all key+model rows."""
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE gemini_key_model_state
                    SET exhausted = 0, exhausted_at = NULL, updated_at = ?
                    WHERE exhausted = 1
                    """,
                    (now,),
                )
                return int(cur.rowcount or 0)
