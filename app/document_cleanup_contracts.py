"""Shared durable cleanup execution phases and permitted scope actions."""

CLEANUP_PHASE_YANDEX = 'yandex'
CLEANUP_PHASE_STORAGE = 'storage_cleanup'
CLEANUP_PHASE_DATABASE = 'database_cleanup'
CLEANUP_EXECUTION_PHASES = frozenset({CLEANUP_PHASE_YANDEX, CLEANUP_PHASE_STORAGE, CLEANUP_PHASE_DATABASE})
CLEANUP_ACTIONS_BY_SCOPE = {
    'document': frozenset({'move', 'delete'}),
    'duplicate_resource': frozenset({'delete'}),
    'source_resource': frozenset({'move'}),
}
