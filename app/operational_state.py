"""Local flow retry state, caches, and CLI preferences; never domain truth."""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from app.local_state import LocalStateStore


def configured_store():
    from app.runtime_config import config_text
    return _initialized_store(Path(config_text("local_state_path")).expanduser())


@lru_cache(maxsize=8)
def _initialized_store(path):
    result = OperationalStateStore(path)
    result.initialize()
    return result


class OperationalStateStore:
    def __init__(self, path: Path | str):
        self.store = LocalStateStore(path)

    def initialize(self):
        self.store.initialize()

    @contextmanager
    def transaction(self):
        with self.store.connect(immediate=True) as conn:
            yield conn

    def get(self, scope, item_id):
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM operational_items WHERE scope=? AND item_id=?",
                (scope, str(item_id)),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def list(self, scope):
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT item_id,payload_json FROM operational_items WHERE scope=? ORDER BY item_id",
                (scope,),
            ).fetchall()
        return {row["item_id"]: json.loads(row["payload_json"]) for row in rows}

    def put(self, scope, item_id, payload, *, conn=None):
        if not isinstance(payload, dict):
            raise ValueError("operational payload must be an object")
        values = (scope, str(item_id), json.dumps(payload, ensure_ascii=False),
                  datetime.now(timezone.utc).isoformat())
        query = """INSERT INTO operational_items(scope,item_id,payload_json,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(scope,item_id) DO UPDATE SET
            payload_json=excluded.payload_json,updated_at=excluded.updated_at"""
        if conn is not None:
            conn.execute(query, values)
        else:
            with self.transaction() as local:
                local.execute(query, values)

    def delete(self, scope, item_id):
        with self.transaction() as conn:
            conn.execute("DELETE FROM operational_items WHERE scope=? AND item_id=?", (scope, str(item_id)))

    def clear(self, scope, *, conn):
        conn.execute("DELETE FROM operational_items WHERE scope=?", (scope,))


def merge_personality_checkpoint(durable, local):
    """A committed domain result wins over stale local work after a crash."""
    if durable is None:
        return local
    result = {**durable, "attempted_models": {},
              "failure_context": durable.get("decision_reason"), "retryable": False}
    if local and all(local.get(key) == durable.get(key) for key in
                     ("source_fingerprint", "prompt_version", "schema_version")):
        result["attempted_models"] = local.get("attempted_models") or {}
    if durable["state"] == "retry_requested":
        result["retryable"] = True
    if local and local.get("state") != "completed":
        if any(local.get(key) != durable.get(key) for key in
               ("source_fingerprint", "prompt_version", "schema_version")):
            return local if local["updated_at"] > durable["updated_at"] else result
        if local["updated_at"] > durable["updated_at"]:
            # A later run can reopen a changed input or explicit reviewed retry.
            if durable["state"] == "retry_requested" or local.get("state") not in {"processing", "pending", "deferred", "failed"}:
                return local
            if local.get("reopened_at", "") >= durable["updated_at"]:
                return local
    return result
