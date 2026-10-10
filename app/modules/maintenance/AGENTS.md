# Maintenance flow rules

Applies to `app/modules/maintenance/`.

- [Storage guidance](guidance/storage.md) owns Yandex traversal, Backblaze checkpoints, and cache behavior; [cleanup guidance](guidance/cleanup.md) owns guarded deletion/moves and locking.
- Storage/cleanup tasks must resume idempotently at safe boundaries, surface item/setup failures, and retain PostgreSQL checkpoints.
- [Operations](../../../docs/operations.md) owns scheduled runners and configuration. The retained `maintenance.monocorpus_meta_evaluate` interactive task is Library-owned; use its [metadata contract](../library/guidance/metadata.md).
