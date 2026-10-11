"""One-message AI decisions; never resolves contextual contributions."""

import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

from app.catalog.command_batch import execute_catalog_batch
from app.catalog.contracts import CatalogConflict, nonblank
from app.catalog.personality_candidates import PERSONALITY_SOURCE_QUERY, compact_text
from app.runtime_config import config_integer

_COMMAND = (Path(__file__).with_suffix('.sql').read_text(encoding='utf-8')
    .replace('{{source_query}}', PERSONALITY_SOURCE_QUERY.replace('{{name_filter}}', "raw_name=request->>'raw_name'"))
    .replace('{{compact_name}}', compact_text('n.raw_name')))


class PersonalityNormalizationStore:
    def save_personality_decision(self, *, source, decision, model, prompt_version, schema_version,
                                  components=None, display_name=None, identity_key=None):
        """Guard the observed source and commit all domain effects in one call."""
        outcome = decision['outcome']
        if outcome not in {'normalized', 'not_person', 'unusable'}:
            raise ValueError('Invalid personality decision')
        if outcome == 'normalized':
            nonblank(display_name, 'display_name')
            nonblank(identity_key, 'identity_key')
            if not components:
                raise ValueError('Normalized decisions require components')
        elif components is not None or display_name is not None or identity_key is not None:
            raise ValueError('Negative decisions cannot contain identity components')
        payload = {
            'raw_name': nonblank(source['raw_name'], 'raw_name'), 'source': source,
            'decision': decision, 'components': components, 'display_name': display_name,
            'identity_key': identity_key, 'model': nonblank(model, 'model'),
            'prompt_version': nonblank(prompt_version, 'prompt_version'),
            'schema_version': nonblank(schema_version, 'schema_version'),
            'now': datetime.now(timezone.utc).isoformat(),
        }
        try:
            return execute_catalog_batch(self.engine, self.schema, _COMMAND, {
                'payload': Json(payload, dumps=lambda value: json.dumps(value, ensure_ascii=False)),
                'lock_timeout': f"{config_integer('postgres', 'lock_timeout_seconds') * 1000}ms",
            }, expected_errors=('MZ001',))
        except psycopg2.Error as exc:
            if exc.pgcode == 'MZ001':
                raise CatalogConflict(exc.diag.message_primary) from None
            raise
