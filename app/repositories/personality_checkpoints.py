"""Compose batched durable decisions with sole local attempt/retry ownership."""

from app.operational_state import OperationalStateStore, merge_personality_checkpoint
from app.repositories.core import utc_now

PERSONALITY_SCOPE = 'library.personality_normalization'


class PersonalityCheckpointRepository:
    def _personality_runtime(self):
        return OperationalStateStore(self._local_state.path)

    def personality_local_names(self):
        return self._personality_runtime().keys(PERSONALITY_SCOPE)

    def personality_checkpoint(self, raw_name, durable_checkpoint):
        return merge_personality_checkpoint(durable_checkpoint,
            self._personality_runtime().get(PERSONALITY_SCOPE, raw_name))

    def save_personality_checkpoint(self, *, raw_name, source_fingerprint, document_count,
            mention_count, source_roles, prompt_version, schema_version, state,
            attempted_models=None, failure_context=None, retryable=False, canonical_id=None,
            completed=False, durable_checkpoint=None):
        """Record local work only; domain outcomes are saved by the catalog command."""
        if state not in {'processing', 'pending', 'deferred', 'failed', 'completed'}:
            raise ValueError('Domain personality decisions require the atomic catalog command')
        now = utc_now()
        payload = dict(raw_name=raw_name, source_fingerprint=source_fingerprint,
            document_count=document_count, mention_count=mention_count, source_roles=sorted(set(source_roles)),
            prompt_version=prompt_version, schema_version=schema_version, state=state,
            attempted_models=attempted_models or {}, failure_context=failure_context, retryable=retryable,
            canonical_id=canonical_id, updated_at=now, completed_at=now if completed else None)
        with self._lock:
            runtime = self._personality_runtime()
            if durable_checkpoint and durable_checkpoint['state'] == 'retry_requested':
                payload['reopened_at'] = durable_checkpoint['updated_at']
            else:
                previous = runtime.get(PERSONALITY_SCOPE, raw_name)
                if previous and previous.get('reopened_at'):
                    payload['reopened_at'] = previous['reopened_at']
            runtime.put(PERSONALITY_SCOPE, raw_name, payload)
