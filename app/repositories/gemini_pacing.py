"""Atomic run admission and exclusive recovery probes in local SQLite."""
import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta

from app.gemini_pacing import (
    GeminiPacingPolicy, GeminiPacingState, PacingOutcome, advance_pacing, pacing_snapshot,
)


class GeminiPacingAdmissionLost(RuntimeError):
    """Preparation lost its admission; reacquire without sending a request."""


class GeminiPacingRepository:
    @staticmethod
    def _pacing_row(conn, policy):
        row = conn.execute("SELECT * FROM gemini_task_pacing WHERE scope_id=?", (policy.scope_id,)).fetchone()
        state = GeminiPacingState(**json.loads(row["state_json"])) if row else GeminiPacingState()
        return row, state

    @staticmethod
    def _write_pacing(conn, policy, state, now_ts):
        conn.execute(
            "UPDATE gemini_task_pacing SET state_json=?,updated_at=? WHERE scope_id=?",
            (json.dumps(asdict(state)), now_ts, policy.scope_id),
        )

    def _claim_pacing(self, conn, policy, *, now_ts, expires_at, lease_token, task_id, run_id, reserve):
        row, state = self._pacing_row(conn, policy)
        barriers = [state.next_start_at, state.cooldown_until]
        if row:
            for kind in ("admission", "probe"):
                if row[f"{kind}_token"]:
                    barriers.append(row[f"{kind}_expires_at"])
        blocked = max((t for t in barriers if t and t > now_ts), default=None)
        if blocked:
            return {"retry_at": blocked, "wait_reason": "pacing"}
        probe = state.mode in {"cooldown", "probe"}
        if not reserve:
            return {"pacing_epoch": state.epoch, "pacing_probe": probe}
        if probe:
            state = replace(state, mode="probe", cooldown_until=None)
        conn.execute(
            """INSERT INTO gemini_task_pacing
                (scope_id,task_id,run_id,state_json,admission_token,admission_expires_at,
                 probe_token,probe_expires_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(scope_id) DO UPDATE SET
                    state_json=excluded.state_json,admission_token=excluded.admission_token,
                    admission_expires_at=excluded.admission_expires_at,
                    probe_token=excluded.probe_token,probe_expires_at=excluded.probe_expires_at,
                    updated_at=excluded.updated_at""",
            (policy.scope_id, task_id, run_id, json.dumps(asdict(state)), lease_token, expires_at,
             lease_token if probe else None, expires_at if probe else None, now_ts),
        )
        result = {"pacing_epoch": state.epoch, "pacing_probe": probe}
        if row is None or probe:
            result["pacing_snapshot"] = {**pacing_snapshot(policy, state), "reason": "probe_started" if probe else "enabled"}
        return result

    def _start_paced_generation(self, conn, policy, *, lease_token, epoch, now_ts, expires_at):
        row, state = self._pacing_row(conn, policy)
        if (not row or row["admission_token"] != lease_token
                or row["admission_expires_at"] <= now_ts or state.epoch != epoch):
            raise GeminiPacingAdmissionLost("Gemini pacing admission lost before generation")
        if state.next_start_at and state.next_start_at > now_ts:
            return state.next_start_at
        state = replace(
            state, last_start_at=now_ts,
            next_start_at=(datetime.fromisoformat(now_ts) + timedelta(
                seconds=policy.intervals_seconds[state.level])).isoformat(),
        )
        self._write_pacing(conn, policy, state, now_ts)
        conn.execute(
            """UPDATE gemini_task_pacing SET admission_token=NULL,admission_expires_at=NULL,
                probe_expires_at=CASE WHEN probe_token=? THEN ? ELSE probe_expires_at END
                WHERE scope_id=?""",
            (lease_token, expires_at, policy.scope_id),
        )
        return None

    def record_gemini_pacing_outcome(
        self, policy: GeminiPacingPolicy, *, quota_domain_id: str, lease_token: str,
        epoch: int, probe: bool, outcome: PacingOutcome, now_ts: str,
    ) -> dict | None:
        with self._runtime_connect(immediate=True) as conn:
            project = conn.execute(
                "SELECT lease_token,lease_expires_at FROM gemini_project_leases WHERE quota_domain_id=?",
                (quota_domain_id,),
            ).fetchone()
            row, state = self._pacing_row(conn, policy)
            if (not project or project["lease_token"] != lease_token
                    or project["lease_expires_at"] <= now_ts or not row or state.epoch != epoch):
                return None
            if probe and (row["probe_token"] != lease_token or row["probe_expires_at"] <= now_ts):
                return None
            updated = advance_pacing(policy, state, outcome, datetime.fromisoformat(now_ts))
            self._write_pacing(conn, policy, updated, now_ts)
            changed = (state.mode, state.level) != (updated.mode, updated.level)
            return {**pacing_snapshot(policy, updated), "reason": outcome,
                    "changed": changed, "probe": probe}
