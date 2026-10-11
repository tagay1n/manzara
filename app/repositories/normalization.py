"""Bounded PostgreSQL discovery and one-message personality decisions."""

from app.catalog.personality_candidates import PERSONALITY_BATCH_SIZE, personality_candidate_batch
from app.catalog.repository import CatalogRepository
from app.repositories.personality_checkpoints import PersonalityCheckpointRepository


class NormalizationRepository(PersonalityCheckpointRepository):
    def personality_candidate_batch(self, *, prompt_version, schema_version, seen_names, local_names, batch_size=PERSONALITY_BATCH_SIZE):
        if self._engine is None:
            raise RuntimeError('Catalog normalization requires the shared PostgreSQL engine')
        return personality_candidate_batch(self._engine, prompt_version=prompt_version, schema_version=schema_version,
            seen_names=seen_names, local_names=local_names, batch_size=batch_size)

    def persist_personality_decision(self, **payload):
        if self._engine is None:
            raise RuntimeError('Catalog normalization requires the shared PostgreSQL engine')
        return CatalogRepository(self._engine, schema=self.schema).save_personality_decision(**payload)
