"""Disposable cached attention snapshots."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.repositories.core import utc_now


class AttentionRepository:
    """Persist derived task-attention state only in local SQLite."""

    def replace_attention_provider_signals(
        self,
        provider_id: str,
        signals: Iterable[Mapping[str, Any]],
        *,
        source_event_id: int,
    ) -> bool:
        normalized = [dict(item) for item in signals]
        now = utc_now()
        changed = False
        with self._runtime_connect(immediate=True) as conn:
            previous = conn.execute(
                "SELECT signal_id,task_id,panel_id,section_id,kind,item_count,label,href,status "
                "FROM attention_signals WHERE provider_id=? ORDER BY signal_id",
                (provider_id,),
            ).fetchall()
            comparable = [
                {
                    "signal_id": str(item["signal_id"]),
                    "task_id": item.get("task_id"),
                    "panel_id": str(item["panel_id"]),
                    "section_id": item.get("section_id"),
                    "kind": str(item["kind"]),
                    "item_count": (
                        int(item.get("count") or 0)
                        if str(item["kind"]) == "count"
                        else None
                    ),
                    "label": str(item["label"]),
                    "href": str(item["href"]),
                    "status": "fresh",
                }
                for item in normalized
                if str(item.get("kind") or "") == "dot"
                or int(item.get("count") or 0) > 0
            ]
            comparable.sort(key=lambda item: item["signal_id"])
            changed = previous != comparable
            conn.execute(
                "DELETE FROM attention_signals WHERE provider_id=?", (provider_id,)
            )
            for item in comparable:
                conn.execute(
                    """INSERT INTO attention_signals (
                           signal_id,provider_id,task_id,panel_id,section_id,kind,
                           item_count,label,href,status,source_event_id,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,'fresh',?,?)""",
                    (
                        item["signal_id"], provider_id, item["task_id"],
                        item["panel_id"], item["section_id"], item["kind"],
                        item["item_count"], item["label"], item["href"],
                        int(source_event_id), now,
                    ),
                )
            conn.execute(
                """INSERT INTO attention_providers (
                       provider_id,status,source_event_id,error_text,updated_at
                   ) VALUES (?,'fresh',?,NULL,?)
                   ON CONFLICT(provider_id) DO UPDATE SET status='fresh',
                       source_event_id=excluded.source_event_id,error_text=NULL,
                       updated_at=excluded.updated_at""",
                (provider_id, int(source_event_id), now),
            )
        return changed

    def set_attention_provider_error(
        self, provider_id: str, error_text: str, *, source_event_id: int
    ) -> None:
        now = utc_now()
        bounded = str(error_text or "")[:1000]
        with self._runtime_connect(immediate=True) as conn:
            conn.execute(
                "UPDATE attention_signals SET status='stale',error_text=?,updated_at=? "
                "WHERE provider_id=?",
                (bounded, now, provider_id),
            )
            conn.execute(
                """INSERT INTO attention_providers (
                       provider_id,status,source_event_id,error_text,updated_at
                   ) VALUES (?,'stale',?,?,?)
                   ON CONFLICT(provider_id) DO UPDATE SET status='stale',
                       source_event_id=excluded.source_event_id,
                       error_text=excluded.error_text,updated_at=excluded.updated_at""",
                (provider_id, int(source_event_id), bounded, now),
            )

    def mark_attention_provider_stale(
        self, provider_id: str, *, source_event_id: int
    ) -> None:
        now = utc_now()
        with self._runtime_connect(immediate=True) as conn:
            conn.execute(
                "UPDATE attention_signals SET status='stale',updated_at=? "
                "WHERE provider_id=?",
                (now, provider_id),
            )
            conn.execute(
                """INSERT INTO attention_providers (
                       provider_id,status,source_event_id,error_text,updated_at
                   ) VALUES (?,'stale',?,NULL,?)
                   ON CONFLICT(provider_id) DO UPDATE SET status='stale',
                       source_event_id=excluded.source_event_id,updated_at=excluded.updated_at""",
                (provider_id, int(source_event_id), now),
            )

    def upsert_attention_dot(
        self,
        signal_id: str,
        *,
        task_id: str,
        panel_id: str,
        label: str,
        href: str,
        source_event_id: int,
    ) -> bool:
        with self._runtime_connect(immediate=True) as conn:
            previous = conn.execute(
                "SELECT task_id,panel_id,label,href,source_event_id FROM attention_signals "
                "WHERE signal_id=?",
                (signal_id,),
            ).fetchone()
            conn.execute(
                """INSERT INTO attention_signals (
                       signal_id,provider_id,task_id,panel_id,section_id,kind,
                       item_count,label,href,status,source_event_id,updated_at
                   ) VALUES (?,?,?,?,NULL,'dot',NULL,?,?,'fresh',?,?)
                   ON CONFLICT(signal_id) DO UPDATE SET label=excluded.label,
                       href=excluded.href,status='fresh',source_event_id=excluded.source_event_id,
                       error_text=NULL,updated_at=excluded.updated_at""",
                (
                    signal_id, "runtime", task_id, panel_id, label, href,
                    int(source_event_id), utc_now(),
                ),
            )
        expected = {
            "task_id": task_id,
            "panel_id": panel_id,
            "label": label,
            "href": href,
            "source_event_id": int(source_event_id),
        }
        return previous != expected

    def delete_attention_signal(self, signal_id: str) -> bool:
        with self._runtime_connect(immediate=True) as conn:
            return conn.execute(
                "DELETE FROM attention_signals WHERE signal_id=?", (signal_id,)
            ).rowcount > 0

    def get_attention_event_cursor(self) -> int:
        with self._runtime_connect() as conn:
            row = conn.execute(
                "SELECT consumed_event_id FROM attention_state WHERE state_id=1"
            ).fetchone()
        return int((row or {}).get("consumed_event_id") or 0)

    def set_attention_event_cursor(self, event_id: int) -> None:
        with self._runtime_connect(immediate=True) as conn:
            conn.execute(
                """INSERT INTO attention_state(state_id,consumed_event_id,updated_at)
                   VALUES (1,?,?) ON CONFLICT(state_id) DO UPDATE SET
                   consumed_event_id=excluded.consumed_event_id,
                   updated_at=excluded.updated_at""",
                (int(event_id), utc_now()),
            )

    def get_attention_snapshot(self) -> dict[str, Any]:
        with self._runtime_connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attention_signals ORDER BY panel_id,signal_id"
            ).fetchall()
            providers = conn.execute(
                "SELECT * FROM attention_providers ORDER BY provider_id"
            ).fetchall()
        signals = []
        for row in rows:
            item = dict(row)
            item["count"] = item.pop("item_count")
            item.pop("error_text", None)
            signals.append(item)
        return {
            "has_attention": bool(signals),
            "signals": signals,
            "providers": {str(row["provider_id"]): dict(row) for row in providers},
        }


__all__ = ["AttentionRepository"]
