"""Yandex resource identity checks shared by discovery and guarded cleanup."""

from collections.abc import Mapping
from typing import Any

from yadisk.exceptions import PathNotFoundError

from app.catalog.contracts import document_md5
from app.document_cleanup_paths import cleanup_source_path, source_path

EMPTY_MD5 = 'd41d8cd98f00b204e9800998ecf8427e'


def resource_value(resource: Any, key: str, default: Any = None) -> Any:
    if isinstance(resource, Mapping):
        return resource.get(key, default)
    return getattr(resource, key, default)


def resource_meta(yadisk: Any, path: str):
    return _resource_meta(yadisk, source_path(path))


def cleanup_source_meta(yadisk: Any, item):
    return _resource_meta(yadisk, cleanup_source_path(item))


def _resource_meta(yadisk: Any, path: str):
    try:
        return yadisk.get_meta(path, fields=['path', 'type', 'size', 'md5', 'resource_id', 'public_url', 'public_key'])
    except PathNotFoundError:
        return None


def verify_resource(resource: Any, *, md5: str, resource_id: str | None = None) -> None:
    """Fail closed if a saved path now addresses a different resource."""
    document_md5(md5)
    if resource is None or resource_value(resource, 'type') != 'file':
        raise RuntimeError('Expected Yandex file is unavailable')
    actual_md5 = str(resource_value(resource, 'md5') or '').lower()
    empty = md5 == EMPTY_MD5 and resource_value(resource, 'size') == 0
    if actual_md5 != md5 and not (empty and not actual_md5):
        raise RuntimeError('Yandex file MD5 changed; inspect the cleanup/source identity')
    if resource_id and resource_value(resource, 'resource_id') != resource_id:
        raise RuntimeError('Yandex resource ID changed; inspect the cleanup/source identity')
