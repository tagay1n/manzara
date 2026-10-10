"""Durable personality decisions composed with sole local retry ownership."""

import json

from app.operational_state import OperationalStateStore, merge_personality_checkpoint
from app.repositories.core import utc_now

PERSONALITY_SCOPE = "library.personality_normalization"


class PersonalityCheckpointRepository:
    def _personality_runtime(self):
        return OperationalStateStore(self._local_state.path)

    def get_personality_checkpoint(self, raw_name):
        with self._connect() as conn:
            durable = conn.execute(
                "SELECT * FROM personality_normalization_checkpoints WHERE raw_name=?", (raw_name,),
            ).fetchone()
        return merge_personality_checkpoint(dict(durable) if durable else None,
            self._personality_runtime().get(PERSONALITY_SCOPE, raw_name))

    def list_personality_checkpoints(self):
        with self._connect() as conn:
            durable = {row["raw_name"]: dict(row) for row in conn.execute(
                "SELECT * FROM personality_normalization_checkpoints ORDER BY raw_name"
            ).fetchall()}
        local = self._personality_runtime().list(PERSONALITY_SCOPE)
        return [merge_personality_checkpoint(durable.get(name), local.get(name))
                for name in sorted(durable.keys() | local.keys())]


    def save_personality_checkpoint(self, *, raw_name, source_fingerprint, document_count,
            mention_count, source_roles, prompt_version, schema_version, state,
            attempted_models=None, failure_context=None, retryable=False, canonical_id=None,
            completed=False, domain_conflict=False):
        negative = state in {"not_person", "unusable"}
        if negative and (canonical_id is not None or retryable or not isinstance(failure_context, str)
                         or not failure_context.strip()):
            raise ValueError("negative personality decisions require a reason and no canonical or automatic retry")
        now = utc_now()
        payload = dict(raw_name=raw_name, source_fingerprint=source_fingerprint,
            document_count=document_count, mention_count=mention_count, source_roles=sorted(set(source_roles)),
            prompt_version=prompt_version, schema_version=schema_version, state=state,
            attempted_models=attempted_models or {}, failure_context=failure_context, retryable=retryable,
            canonical_id=canonical_id, updated_at=now, completed_at=now if completed else None)
        with self._lock:
            with self._connect() as conn:
                if negative or domain_conflict:
                    conn.execute("SET TRANSACTION READ WRITE")
                old = conn.execute("SELECT * FROM personality_normalization_checkpoints WHERE raw_name=?", (raw_name,)).fetchone()
                if negative or domain_conflict:
                    decisions = [(model, value) for model, value in (attempted_models or {}).items()
                                 if isinstance(value, dict) and value.get("kind") == "decision"]
                    model, evidence = decisions[-1] if decisions else (None, {})
                    conn.execute("""INSERT INTO personality_normalization_checkpoints
                        (raw_name,source_fingerprint,document_count,mention_count,source_roles,prompt_version,
                         schema_version,state,decision_reason,successful_model,decision_evidence,canonical_id,updated_at,completed_at)
                        VALUES(?,?,?,?,?::jsonb,?,?,?,?,?,?::jsonb,?,?,?) ON CONFLICT(raw_name) DO UPDATE SET
                        source_fingerprint=excluded.source_fingerprint,document_count=excluded.document_count,
                        mention_count=excluded.mention_count,source_roles=excluded.source_roles,
                        prompt_version=excluded.prompt_version,schema_version=excluded.schema_version,state=excluded.state,
                        decision_reason=excluded.decision_reason,successful_model=excluded.successful_model,
                        decision_evidence=excluded.decision_evidence,canonical_id=excluded.canonical_id,
                        updated_at=excluded.updated_at,completed_at=excluded.completed_at""",
                        (raw_name, source_fingerprint, document_count, mention_count, json.dumps(payload["source_roles"]),
                         prompt_version, schema_version, state, failure_context, model, json.dumps(evidence),
                         canonical_id, now, payload["completed_at"]))
            if old and old["state"] == "retry_requested":
                payload["reopened_at"] = old["updated_at"]
            else:
                previous = self._personality_runtime().get(PERSONALITY_SCOPE, raw_name)
                if previous and previous.get("reopened_at"):
                    payload["reopened_at"] = previous["reopened_at"]
            if negative or domain_conflict:
                payload.update(state="completed", failure_context=None, retryable=False, canonical_id=None,
                    attempted_models={model: value for model, value in (attempted_models or {}).items()
                                      if not isinstance(value, dict) or value.get("kind") != "decision"})
            self._personality_runtime().put(PERSONALITY_SCOPE, raw_name, payload)
