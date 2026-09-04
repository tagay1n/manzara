"""PostgreSQL persistence for the singleton task conveyor."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.repositories.core import utc_now
from app.runtime_states import CONVEYOR_RUN_ACTIVE_STATUSES


class ConveyorRevisionConflict(RuntimeError):
    """Raised when a stale conveyor definition revision is submitted."""


class ConveyorRepository:
    """Persistence operations for conveyor definitions and executions."""

    @staticmethod
    def _decode_json(value: Any, fallback: Any) -> Any:
        try:
            return json.loads(str(value or ""))
        except Exception:
            return fallback

    def get_conveyor_definition(self) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conveyor_definitions WHERE conveyor_id = 'default'"
            ).fetchone()
        if not row:
            return {"conveyor_id": "default", "revision": 0, "stages": []}
        payload = dict(row)
        stages = self._decode_json(payload.pop("stages_json", "[]"), [])
        payload["stages"] = stages if isinstance(stages, list) else []
        return payload

    def get_conveyor_snapshot(self) -> Dict[str, Any]:
        """Return definition, latest relevant run, and its items in one query."""
        with self._connect() as conn:
            row = conn.execute(
                """
                WITH selected_run AS (
                    SELECT *
                    FROM conveyor_runs
                    ORDER BY
                        CASE WHEN status IN ('starting', 'running') THEN 0 ELSE 1 END,
                        conveyor_run_id DESC
                    LIMIT 1
                )
                SELECT
                    (SELECT row_to_json(definition_row)
                     FROM conveyor_definitions AS definition_row
                     WHERE conveyor_id = 'default') AS definition,
                    (SELECT row_to_json(run_row)
                     FROM selected_run AS run_row) AS run,
                    COALESCE(
                        (
                            SELECT json_agg(item_row ORDER BY stage_order, task_order)
                            FROM (
                                SELECT cri.*, r.progress_json
                                FROM conveyor_run_items cri
                                LEFT JOIN runs r ON r.run_id = cri.task_run_id
                                WHERE cri.conveyor_run_id = (
                                    SELECT conveyor_run_id FROM selected_run
                                )
                            ) AS item_row
                        ),
                        '[]'::json
                    ) AS items
                """
            ).fetchone() or {}

        definition_row = row.get("definition")
        if definition_row:
            definition = dict(definition_row)
            stages = self._decode_json(definition.pop("stages_json", "[]"), [])
            definition["stages"] = stages if isinstance(stages, list) else []
        else:
            definition = {"conveyor_id": "default", "revision": 0, "stages": []}

        run_row = row.get("run")
        run = self._conveyor_run_payload(run_row) if run_row else None
        items: List[Dict[str, Any]] = []
        for raw_item in row.get("items") or []:
            item = dict(raw_item)
            output = self._decode_json(item.pop("output_json", "{}"), {})
            progress = self._decode_json(item.pop("progress_json", "{}"), {})
            item["output"] = output if isinstance(output, dict) else {}
            item["progress"] = progress if isinstance(progress, dict) else {}
            if item.get("meaningful") is not None:
                item["meaningful"] = bool(item["meaningful"])
            items.append(item)
        return {"definition": definition, "run": run, "items": items}

    def save_conveyor_definition(
        self,
        *,
        expected_revision: int,
        stages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        now = utc_now()
        encoded = json.dumps(stages, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO conveyor_definitions (
                        conveyor_id, revision, stages_json, created_at, updated_at
                    ) VALUES ('default', 0, '[]', ?, ?)
                    ON CONFLICT(conveyor_id) DO NOTHING
                    """,
                    (now, now),
                )
                row = conn.execute(
                    """
                    SELECT revision
                    FROM conveyor_definitions
                    WHERE conveyor_id = 'default'
                    FOR UPDATE
                    """
                ).fetchone()
                current_revision = int((row or {}).get("revision") or 0)
                if current_revision != int(expected_revision):
                    raise ConveyorRevisionConflict(
                        f"Expected conveyor revision {expected_revision}, "
                        f"found {current_revision}"
                    )
                next_revision = current_revision + 1
                conn.execute(
                    """
                    UPDATE conveyor_definitions
                    SET revision = ?, stages_json = ?, updated_at = ?
                    WHERE conveyor_id = 'default'
                    """,
                    (next_revision, encoded, now),
                )
                active = conn.execute(
                    """
                    SELECT conveyor_run_id
                    FROM conveyor_runs
                    WHERE status IN ('starting', 'running')
                    ORDER BY conveyor_run_id DESC
                    LIMIT 1
                    FOR UPDATE
                    """
                ).fetchone()
                if active:
                    run_id = int(active["conveyor_run_id"])
                    locked_rows = conn.execute(
                        """
                        SELECT item_id
                        FROM conveyor_run_items
                        WHERE conveyor_run_id = ? AND status <> 'pending'
                        """,
                        (run_id,),
                    ).fetchall()
                    locked_ids = {str(item["item_id"]) for item in locked_rows}
                    conn.execute(
                        """
                        DELETE FROM conveyor_run_items
                        WHERE conveyor_run_id = ? AND status = 'pending'
                        """,
                        (run_id,),
                    )
                    for stage_order, stage in enumerate(stages):
                        stage_id = str(stage["stage_id"])
                        for task_order, item in enumerate(stage["items"]):
                            item_id = str(item["item_id"])
                            if item_id in locked_ids:
                                continue
                            conn.execute(
                                """
                                INSERT INTO conveyor_run_items (
                                    conveyor_run_id, item_id, stage_id,
                                    stage_order, task_order, task_id, status
                                ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                                """,
                                (
                                    run_id,
                                    item_id,
                                    stage_id,
                                    stage_order,
                                    task_order,
                                    str(item["task_id"]),
                                ),
                            )
        return self.get_conveyor_definition()

    def get_active_conveyor_run(self) -> Optional[Dict[str, Any]]:
        placeholders = ", ".join("?" for _ in CONVEYOR_RUN_ACTIVE_STATUSES)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM conveyor_runs
                WHERE status IN ({placeholders})
                ORDER BY conveyor_run_id DESC
                LIMIT 1
                """,
                CONVEYOR_RUN_ACTIVE_STATUSES,
            ).fetchone()
        return self._conveyor_run_payload(row) if row else None

    def get_latest_conveyor_run(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM conveyor_runs
                ORDER BY conveyor_run_id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._conveyor_run_payload(row) if row else None

    def get_conveyor_run(self, conveyor_run_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conveyor_runs WHERE conveyor_run_id = ?",
                (int(conveyor_run_id),),
            ).fetchone()
        return self._conveyor_run_payload(row) if row else None

    @staticmethod
    def _conveyor_run_payload(row: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(row)
        payload["stop_requested"] = bool(payload.get("stop_requested"))
        return payload

    def create_conveyor_run(self) -> int:
        definition = self.get_conveyor_definition()
        stages = definition.get("stages") or []
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    INSERT INTO conveyor_runs (
                        definition_revision, status, started_at, stop_requested
                    ) VALUES (?, 'starting', ?, 0)
                    RETURNING conveyor_run_id
                    """,
                    (int(definition.get("revision") or 0), now),
                ).fetchone()
                run_id = int((row or {})["conveyor_run_id"])
                for stage_order, stage in enumerate(stages):
                    for task_order, item in enumerate(stage.get("items") or []):
                        conn.execute(
                            """
                            INSERT INTO conveyor_run_items (
                                conveyor_run_id, item_id, stage_id,
                                stage_order, task_order, task_id, status
                            ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                            """,
                            (
                                run_id,
                                str(item["item_id"]),
                                str(stage["stage_id"]),
                                stage_order,
                                task_order,
                                str(item["task_id"]),
                            ),
                        )
        return run_id

    def list_conveyor_run_items(self, conveyor_run_id: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT cri.*, r.progress_json
                FROM conveyor_run_items cri
                LEFT JOIN runs r ON r.run_id = cri.task_run_id
                WHERE cri.conveyor_run_id = ?
                ORDER BY cri.stage_order, cri.task_order
                """,
                (int(conveyor_run_id),),
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            output = self._decode_json(payload.pop("output_json", "{}"), {})
            progress = self._decode_json(payload.pop("progress_json", "{}"), {})
            payload["output"] = output if isinstance(output, dict) else {}
            payload["progress"] = progress if isinstance(progress, dict) else {}
            if payload.get("meaningful") is not None:
                payload["meaningful"] = bool(payload["meaningful"])
            result.append(payload)
        return result

    def set_conveyor_run_running(self, conveyor_run_id: int) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE conveyor_runs SET status = 'running' WHERE conveyor_run_id = ?",
                    (int(conveyor_run_id),),
                )

    def claim_next_conveyor_stage(self, conveyor_run_id: int) -> List[Dict[str, Any]]:
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                run = conn.execute(
                    """
                    SELECT stop_requested, status FROM conveyor_runs
                    WHERE conveyor_run_id = ? FOR UPDATE
                    """,
                    (int(conveyor_run_id),),
                ).fetchone()
                if not run or bool(run.get("stop_requested")):
                    return []
                stage = conn.execute(
                    """
                    SELECT MIN(stage_order) AS stage_order
                    FROM conveyor_run_items
                    WHERE conveyor_run_id = ? AND status = 'pending'
                    """,
                    (int(conveyor_run_id),),
                ).scalar()
                if stage is None:
                    return []
                conn.execute(
                    """
                    UPDATE conveyor_run_items
                    SET status = 'starting', started_at = ?
                    WHERE conveyor_run_id = ?
                      AND stage_order = ? AND status = 'pending'
                    """,
                    (now, int(conveyor_run_id), int(stage)),
                )
                rows = conn.execute(
                    """
                    SELECT * FROM conveyor_run_items
                    WHERE conveyor_run_id = ? AND stage_order = ?
                    ORDER BY task_order
                    """,
                    (int(conveyor_run_id), int(stage)),
                ).fetchall()
        return [dict(row) for row in rows]

    def set_conveyor_item_running(
        self,
        conveyor_run_id: int,
        item_id: str,
        task_run_id: int,
    ) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE conveyor_run_items
                    SET status = 'running', task_run_id = ?
                    WHERE conveyor_run_id = ? AND item_id = ?
                    """,
                    (int(task_run_id), int(conveyor_run_id), str(item_id)),
                )

    def finish_conveyor_item(
        self,
        conveyor_run_id: int,
        item_id: str,
        *,
        status: str,
        meaningful: Optional[bool],
        output: Dict[str, Any],
        error_text: Optional[str] = None,
    ) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE conveyor_run_items
                    SET status = ?, meaningful = ?, output_json = ?,
                        error_text = ?, finished_at = ?
                    WHERE conveyor_run_id = ? AND item_id = ?
                    """,
                    (
                        str(status),
                        None if meaningful is None else int(bool(meaningful)),
                        json.dumps(output or {}, ensure_ascii=False),
                        str(error_text or "").strip() or None,
                        utc_now(),
                        int(conveyor_run_id),
                        str(item_id),
                    ),
                )

    def request_conveyor_stop(self, conveyor_run_id: int) -> bool:
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE conveyor_runs SET stop_requested = 1
                    WHERE conveyor_run_id = ? AND status IN ('starting', 'running')
                    """,
                    (int(conveyor_run_id),),
                )
                conn.execute(
                    """
                    UPDATE conveyor_run_items
                    SET status = 'canceled', finished_at = ?
                    WHERE conveyor_run_id = ? AND status = 'pending'
                    """,
                    (utc_now(), int(conveyor_run_id)),
                )
        return int(cur.rowcount or 0) > 0

    def finish_conveyor_run(
        self,
        conveyor_run_id: int,
        *,
        status: str,
        outcome: Optional[str] = None,
        error_text: Optional[str] = None,
    ) -> None:
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE conveyor_run_items
                    SET status = 'canceled', finished_at = ?
                    WHERE conveyor_run_id = ? AND status IN ('pending', 'starting')
                    """,
                    (now, int(conveyor_run_id)),
                )
                conn.execute(
                    """
                    UPDATE conveyor_runs
                    SET status = ?, outcome = ?, error_text = ?, finished_at = ?
                    WHERE conveyor_run_id = ?
                    """,
                    (
                        str(status),
                        str(outcome or "").strip() or None,
                        str(error_text or "").strip() or None,
                        now,
                        int(conveyor_run_id),
                    ),
                )

    def recover_active_conveyor_runs(self) -> int:
        now = utc_now()
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE conveyor_runs
                    SET status = 'failed', finished_at = ?,
                        error_text = 'Recovered after Manzara restart; rerun the conveyor.'
                    WHERE status IN ('starting', 'running')
                    """,
                    (now,),
                )
                conn.execute(
                    """
                    UPDATE conveyor_run_items
                    SET status = 'canceled', finished_at = ?
                    WHERE status IN ('pending', 'starting', 'running')
                      AND conveyor_run_id IN (
                          SELECT conveyor_run_id FROM conveyor_runs
                          WHERE status = 'failed' AND finished_at = ?
                      )
                    """,
                    (now, now),
                )
        return int(cur.rowcount or 0)
