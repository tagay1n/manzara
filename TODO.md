# Active work

## Current priorities

- Adapt backend reads, writes, and workers to the migrated PostgreSQL catalog. Audit legacy SQL/ORM assumptions before treating workflows as ready. See [catalog model](docs/catalog-model.md).
- Implement a rich operations CLI after its scope is defined. Preserve backend-owned decisions, safe stop/resume, bounded logs, and structured artifacts.

## Older requests needing owner reprioritization

These came from the previous backlog; their current necessity is unconfirmed.

- Rename the database schema; move `gec-annotations-filter`.
- Revisit task hierarchy and concurrency.
- Review which metadata facets should be English; descriptions currently follow document language.
- Review remote `upstream_meta` / Schema.org buckets and retention before deleting anything.
- Improve OCR handling for image-only DOC/RTF and DjVu; sample FB2/EPUB regressions.

Removed completed converter requests and historical cohort counts. Upstream metadata already has a database owner; a notification panel belongs to the retired web design.
