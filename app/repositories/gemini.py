from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from app.repositories.core import utc_now
from app.gemini_pacing import GeminiPacingPolicy
from app.repositories.gemini_pacing import GeminiPacingRepository


def _ready_candidate(
    rows: List[Dict[str, Any]],
    models: Sequence[str],
    now_ts: str,
    busy_accounts: set[str],
) -> tuple[Dict[str, Any] | None, str | None]:
    waits = []
    for model in models:
        ready = []
        for row in rows:
            if row["model_name"] != model or row["exhausted"]:
                continue
            barriers = [
                row.get(field)
                for field in (
                    "pause_until",
                    "quota_until",
                    "cooldown_until",
                    "next_request_at",
                )
            ]
            if row["lease_token"]:
                barriers.append(row["lease_expires_at"])
            blocked_until = max(
                (ts for ts in barriers if ts and ts > now_ts), default=None
            )
            if blocked_until:
                waits.append(blocked_until)
            else:
                ready.append(row)
        if ready:
            return min(
                ready,
                key=lambda row: (
                    row["account_id"] in busy_accounts,
                    row["last_acquired_at"] or "",
                    row["quota_domain_id"],
                    row["key_id"],
                ),
            ), None
    return None, min(waits) if waits else None


class GeminiRepository(GeminiPacingRepository):
    """Machine-local SQLite operations for Gemini coordination."""

    def claim_gemini_ready_request(
        self,
        *,
        models: Sequence[str],
        key_ids: Sequence[str],
        now_ts: str,
        cooldown_until: str,
        expires_at: str,
        lease_token: str,
        task_id: Optional[str],
        run_id: Optional[int],
        worker_id: str,
        reserve: bool = True,
        pacing_policy: GeminiPacingPolicy | None = None,
    ) -> Dict[str, Any]:
        """Select and claim a ready project/model in one cross-process transaction."""
        if not models or not key_ids:
            return {"retry_at": None}
        with self._runtime_connect(immediate=True) as conn:
            cursor = conn.execute(
                "SELECT last_model FROM gemini_scheduler_cursor WHERE cursor_id=1"
            ).fetchone()
            ordered = list(dict.fromkeys(models))
            last = cursor["last_model"] if cursor else None
            if last in ordered:
                start = ordered.index(last) + 1
                ordered = ordered[start:] + ordered[:start]
            placeholders = ",".join("?" for _ in key_ids)
            rows = conn.execute(
                f"""SELECT k.*, s.model_name, s.exhausted, s.cooldown_until,
                    m.pause_until, q.cooldown_until AS quota_until,
                    p.lease_token, p.lease_expires_at, p.last_acquired_at,
                    spacing.next_request_at
                    FROM gemini_keys k JOIN gemini_key_model_state s USING(key_id)
                    LEFT JOIN gemini_model_runtime m USING(model_name)
                    LEFT JOIN gemini_quota_domain_model_state q
                      ON q.quota_domain_id=k.quota_domain_id AND q.model_name=s.model_name
                    LEFT JOIN gemini_project_leases p USING(quota_domain_id)
                    LEFT JOIN gemini_project_model_spacing spacing
                      ON spacing.quota_domain_id=k.quota_domain_id AND spacing.model_name=s.model_name
                    WHERE k.active=1 AND k.key_id IN ({placeholders})""",
                key_ids,
            ).fetchall()
            busy_accounts = {
                r["account_id"]
                for r in conn.execute(
                    """SELECT DISTINCT k.account_id FROM gemini_project_leases p
                        JOIN gemini_keys k USING(quota_domain_id)
                        WHERE p.lease_token IS NOT NULL AND p.lease_expires_at > ?""",
                    (now_ts,),
                ).fetchall()
            }
            key, retry_at = _ready_candidate(rows, ordered, now_ts, busy_accounts)
            if key is None:
                return {"retry_at": retry_at}
            pacing = {}
            if pacing_policy is not None:
                pacing = self._claim_pacing(
                    conn, pacing_policy, now_ts=now_ts, expires_at=expires_at,
                    lease_token=lease_token, task_id=task_id, run_id=run_id, reserve=reserve,
                )
                if "retry_at" in pacing:
                    return pacing
            if not reserve:
                return {**key, **pacing, "retry_at": None}
            model = key["model_name"]
            domain = key["quota_domain_id"]
            conn.execute(
                """INSERT INTO gemini_project_leases
                    (quota_domain_id, lease_token, lease_expires_at, last_acquired_at,
                     task_id, run_id, worker_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(quota_domain_id) DO UPDATE SET
                    lease_token=excluded.lease_token, lease_expires_at=excluded.lease_expires_at,
                    last_acquired_at=excluded.last_acquired_at, task_id=excluded.task_id,
                    run_id=excluded.run_id, worker_id=excluded.worker_id""",
                (domain, lease_token, expires_at, now_ts, task_id, run_id, worker_id),
            )
            conn.execute(
                """INSERT INTO gemini_project_model_spacing VALUES (?, ?, ?)
                    ON CONFLICT(quota_domain_id, model_name) DO UPDATE
                    SET next_request_at=excluded.next_request_at""",
                (domain, model, cooldown_until),
            )
            conn.execute(
                """UPDATE gemini_key_model_state SET last_used_at=?, cooldown_until=?,
                    attempts_total=attempts_total+1, attempts_cycle=attempts_cycle+1,
                    updated_at=? WHERE key_id=? AND model_name=?""",
                (now_ts, cooldown_until, now_ts, key["key_id"], model),
            )
            conn.execute(
                """INSERT INTO gemini_scheduler_cursor VALUES (1, ?)
                    ON CONFLICT(cursor_id) DO UPDATE SET last_model=excluded.last_model""",
                (model,),
            )
            return {**key, **pacing, "lease_token": lease_token, "retry_at": None}

    def record_gemini_generation_start(
        self, quota_domain_id: str, model_name: str, *, key_id: str, lease_token: str,
        now_ts: str, next_request_at: str, expires_at: str,
        pacing_policy: GeminiPacingPolicy | None = None, pacing_epoch: int = 0,
    ) -> bool | str:
        """Advance spacing at generation after preparation, under the project lease."""
        with self._runtime_connect(immediate=True) as conn:
            renewed = conn.execute(
                """UPDATE gemini_project_leases SET lease_expires_at=?
                   WHERE quota_domain_id=? AND lease_token=? AND lease_expires_at > ?""",
                (expires_at, quota_domain_id, lease_token, now_ts),
            ).rowcount
            if not renewed:
                return False
            if pacing_policy is not None:
                wait_until = self._start_paced_generation(
                    conn, pacing_policy, lease_token=lease_token, epoch=pacing_epoch, now_ts=now_ts,
                    expires_at=expires_at,
                )
                if wait_until:
                    return wait_until
            conn.execute(
                """UPDATE gemini_project_model_spacing SET next_request_at=?
                   WHERE quota_domain_id=? AND model_name=?""",
                (next_request_at, quota_domain_id, model_name),
            )
            conn.execute(
                """UPDATE gemini_key_model_state SET cooldown_until=?
                   WHERE key_id=? AND model_name=?""",
                (next_request_at, key_id, model_name),
            )
            return True

    def renew_gemini_project_lease(
        self,
        quota_domain_id: str,
        lease_token: str,
        *,
        expires_at: str,
        now_ts: str,
    ) -> bool:
        with self._runtime_connect(immediate=True) as conn:
            renewed = bool(
                conn.execute(
                    """UPDATE gemini_project_leases SET lease_expires_at=?
                   WHERE quota_domain_id=? AND lease_token=?""",
                    (expires_at, quota_domain_id, lease_token),
                ).rowcount
            )
            if renewed:
                conn.execute(
                    """UPDATE gemini_task_pacing SET
                        admission_expires_at=CASE WHEN admission_token=? THEN ? ELSE admission_expires_at END,
                        probe_expires_at=CASE WHEN probe_token=? THEN ? ELSE probe_expires_at END
                        WHERE admission_token=? OR probe_token=?""",
                    (lease_token, expires_at, lease_token, expires_at, lease_token, lease_token),
                )
            return renewed

    def release_gemini_project_lease(
        self, quota_domain_id: str, lease_token: str
    ) -> bool:
        with self._runtime_connect(immediate=True) as conn:
            conn.execute(
                """UPDATE gemini_task_pacing SET
                    admission_token=CASE WHEN admission_token=? THEN NULL ELSE admission_token END,
                    admission_expires_at=CASE WHEN admission_token=? THEN NULL ELSE admission_expires_at END,
                    probe_token=CASE WHEN probe_token=? THEN NULL ELSE probe_token END,
                    probe_expires_at=CASE WHEN probe_token=? THEN NULL ELSE probe_expires_at END
                    WHERE admission_token=? OR probe_token=?""",
                (lease_token,) * 6,
            )
            return bool(
                conn.execute(
                    """UPDATE gemini_project_leases SET lease_token=NULL, lease_expires_at=NULL,
                    task_id=NULL, run_id=NULL, worker_id=NULL
                    WHERE quota_domain_id=? AND lease_token=?""",
                    (quota_domain_id, lease_token),
                ).rowcount
            )

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
                    conn.execute(
                        "UPDATE gemini_keys SET active=0, updated_at=?", (now,)
                    )
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
