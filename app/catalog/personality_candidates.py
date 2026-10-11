"""SQL-aggregated personality sources, including the existing fingerprint contract."""

from pathlib import Path

from sqlalchemy import text

from app.catalog.contracts import integer

PERSONALITY_BATCH_SIZE = 200
# Match Python str.split(), including its four C0 separators, before NFC.
_WHITESPACE = r"U&'[\0009-\000D\001C-\0020\0085\00A0\1680\2000-\200A\2028\2029\202F\205F\3000]+'"


def compact_text(column):
    return f"normalize(btrim(regexp_replace({column},{_WHITESPACE},' ','g')),NFC)"


PERSONALITY_SOURCE_QUERY = (Path(__file__).with_suffix('.sql').read_text(encoding='utf-8')
    .replace('{{compact_name}}', compact_text('n.raw_name'))
    .replace('{{compact_language}}', compact_text('l.language')))
_DISCOVERY = """
SELECT candidate.*, CASE WHEN checkpoint.raw_name IS NOT NULL THEN to_jsonb(checkpoint) END AS durable_checkpoint
FROM ({{sources}}) candidate
LEFT JOIN personality_normalization_checkpoints checkpoint USING(raw_name)
WHERE NOT COALESCE(checkpoint.source_fingerprint=candidate.source_fingerprint
    AND (checkpoint.state='succeeded'
        OR (checkpoint.state IN ('not_person','unusable')
            AND checkpoint.prompt_version=:prompt_version AND checkpoint.schema_version=:schema_version)),false)
ORDER BY CASE WHEN checkpoint.raw_name IS NULL AND NOT (candidate.raw_name=ANY(:local_names)) THEN 0 ELSE 1 END,
    casefold(candidate.raw_name COLLATE pg_unicode_fast) COLLATE "C", candidate.raw_name COLLATE "C"
LIMIT :batch_size
""".replace('{{sources}}', PERSONALITY_SOURCE_QUERY.replace('{{name_filter}}', 'NOT (raw_name=ANY(:seen_names))'))


def personality_candidate_batch(engine, *, prompt_version, schema_version, seen_names, local_names,
                                batch_size=PERSONALITY_BATCH_SIZE):
    integer(batch_size, 'batch_size')
    if batch_size > PERSONALITY_BATCH_SIZE:
        raise ValueError(f'batch_size must not exceed {PERSONALITY_BATCH_SIZE}')
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(_DISCOVERY), {
            'prompt_version': prompt_version, 'schema_version': schema_version,
            'seen_names': list(seen_names), 'local_names': list(local_names), 'batch_size': batch_size,
        }).mappings()]
