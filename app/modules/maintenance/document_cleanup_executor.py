"""Guarded Yandex mutation boundary for persisted document cleanup plans."""

from __future__ import annotations

from typing import Any, Mapping

from app.catalog.contracts import document_md5, integer
from app.document_cleanup_contracts import CLEANUP_ACTIONS_BY_SCOPE
from app.document_cleanup_paths import cleanup_source_path, source_path
from app.document_resources import cleanup_source_meta, resource_meta, verify_resource


def execute_yandex_cleanup(item: Mapping[str, Any], *, yadisk: Any) -> None:
    """Revalidate a persisted plan's remote identity immediately before mutation."""
    integer(item.get('cleanup_id'), 'cleanup_id')
    if item.get('status') != 'running':
        raise ValueError('Yandex cleanup must have a claimed persisted plan')
    md5 = document_md5(item.get('md5'))
    source = cleanup_source_path(item)
    scope = item.get('scope')
    if item.get('action') not in CLEANUP_ACTIONS_BY_SCOPE.get(scope, ()):
        raise ValueError('Unsupported cleanup scope')
    meta = cleanup_source_meta(yadisk, item)
    verify_resource(meta, md5=md5, resource_id=item.get('source_resource_id'))
    action = item.get('action')
    if action == 'delete':
        if scope == 'duplicate_resource':
            canonical = source_path(item.get('evidence_json', {}).get('canonical_path'))
            if canonical == source:
                raise RuntimeError('Duplicate cleanup cannot delete its canonical resource')
            verify_resource(resource_meta(yadisk, canonical), md5=md5)
        yadisk.remove(source, permanently=True)
        return
    if action == 'move':
        target = source_path(item.get('target_path'))
        if target == source or resource_meta(yadisk, target) is not None:
            raise RuntimeError('Cleanup move target already exists; inspect it before retrying')
        yadisk.move(source, target, overwrite=False)
        return
    raise ValueError(f'Unsupported cleanup action: {action!r}')


__all__ = ['execute_yandex_cleanup']
