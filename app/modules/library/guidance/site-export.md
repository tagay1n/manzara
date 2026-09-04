# Static Library export

The `library.site_export` task is the only supported database boundary for the
separate static-site repository. The site must consume the versioned bundle and
must not query Manzara tables directly.

## Ownership and safety

- Manzara owns publication eligibility, privacy checks, Schema.org validation,
  canonical entity resolution, collection/classification joins, and public asset
  URL construction.
- Export only complete, unrestricted documents whose current primary object is
  verified and belongs to the configured public document bucket.
- Validate every `metadata.schema_org` object against the current Library
  contract. Retained invalid metadata is operational repair state, not public
  content.
- Never export source paths, upstream evidence, private/encrypted URLs, expiring
  signed URLs, storage credentials, checksums used for storage synchronization,
  or workflow/evaluation state.
- Read candidates and normalization aliases in one repeatable-read transaction.
- A stopped or failed run must not publish a partial bundle.

## Version 1 bundle

The task writes
`$MANZARA_ARTIFACTS_ROOT/durable/library/site-exports/run-<run-id>/library-export-v1.tar.gz`
(default artifacts root: `~/.manzara`). The tarball contains, in stable order:

1. `manifest.json`
2. `documents.jsonl`
3. `entities.jsonl`
4. `collections.jsonl`
5. `classifications.jsonl`
6. `redirects.jsonl`

The manifest identifies `manzara-library-export` version `1`, the active
metadata contract, record counts, SHA-256 checksums, a semantic bundle revision,
and exclusion counters. JSONL records and arrays use deterministic ordering.

Document records keep canonical JSON-LD under `work` and place build-friendly
data under `file`, `relations`, `facets`, and optional `preview`. Database IDs are
serialized as opaque namespaced strings. MD5 remains the stable document
identity and a short MD5 suffix keeps public paths collision-safe.

Only reviewed, linked, active personality and publisher canonicals receive
standalone entity IDs. Unresolved contributor names remain visible on documents
without creating unstable entity pages. One document may belong to at most one
exported collection and one canonical classification.

## Compatibility

Version 1 permits additive optional fields and new ignorable records. Removing
a required field, changing a field type or meaning, or changing identity and
relationship semantics requires a new export version. During a breaking
transition, emit the old and new versions in parallel rather than changing
version 1 in place.
