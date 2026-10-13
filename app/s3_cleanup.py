"""Permanent S3 deletion; callers own key selection and domain guards."""

from __future__ import annotations

from itertools import islice
from typing import Any, Callable, Iterable, Iterator

from app.runtime_config import config_integer

Version = tuple[str, str]


def iter_s3_versions(s3: Any, bucket: str, prefix: str, *, kinds: tuple[str, ...]) -> Iterator[Version]:
    parameters = {"Bucket": bucket, "Prefix": prefix,
                  "MaxKeys": config_integer("maintenance", "s3_listing_page_size", maximum=1000)}
    seen_markers = set()
    while True:
        page = s3.list_object_versions(**parameters)
        for kind in kinds:
            for item in page.get(kind, []):
                version_id = item.get("VersionId")
                if not isinstance(version_id, str) or not version_id.strip():
                    raise RuntimeError(f"S3 object has no version ID bucket={bucket} key={item['Key']}")
                yield item["Key"], version_id
        truncated = page.get("IsTruncated", False)
        if type(truncated) is not bool:
            raise RuntimeError("S3 version pagination requires a boolean IsTruncated")
        if not truncated:
            return
        marker = (page.get("NextKeyMarker"), page.get("NextVersionIdMarker"))
        if not marker[0] or marker in seen_markers:
            raise RuntimeError("S3 version pagination did not advance")
        seen_markers.add(marker)
        parameters["KeyMarker"] = marker[0]
        if marker[1] is None:
            parameters.pop("VersionIdMarker", None)
        else:
            parameters["VersionIdMarker"] = marker[1]


def delete_s3_versions(s3: Any, bucket: str, versions: Iterable[Version]) -> int:
    """Delete explicit identities only; fail on missing or partial acknowledgements."""
    pending = iter(versions)
    deleted = 0
    while batch := tuple(islice(pending, 1000)):
        if any(not isinstance(version_id, str) or not version_id.strip() for _, version_id in batch):
            raise RuntimeError("Permanent S3 deletion requires explicit version IDs")
        response = s3.delete_objects(Bucket=bucket, Delete={
            "Objects": [{"Key": key, "VersionId": version_id} for key, version_id in batch],
            "Quiet": False,
        })
        if response.get("Errors"):
            raise RuntimeError(f"Permanent S3 deletion failed bucket={bucket}: {response['Errors']}")
        confirmed = {(item.get("Key"), item.get("VersionId")) for item in response.get("Deleted", [])}
        if confirmed != set(batch):
            raise RuntimeError(f"Permanent S3 deletion was not fully acknowledged bucket={bucket}")
        deleted += len(batch)
    return deleted


def purge_s3_history(
    s3: Any, bucket: str, prefix: str, *, key_matches: Callable[[str], bool],
    before_delete: Callable[[str, tuple[Version, ...]], None] | None = None,
) -> int:
    """Remove bounded batches of data before markers, then verify selected keys."""
    def batch(kinds: tuple[str, ...]) -> tuple[Version, ...]:
        return tuple(sorted(islice(
            (version for version in iter_s3_versions(s3, bucket, prefix, kinds=kinds)
             if key_matches(version[0])), 1000)))

    deleted = 0
    for kind in ("Versions", "DeleteMarkers"):
        previous = ()
        while versions := batch((kind,)):
            if versions == previous:
                raise RuntimeError(f"S3 versions remain after deletion bucket={bucket} prefix={prefix}")
            if kind == "DeleteMarkers" and batch(("Versions",)):
                raise RuntimeError(f"S3 data reappeared before marker removal bucket={bucket} prefix={prefix}")
            if before_delete is not None:
                before_delete(kind, versions)
            deleted += delete_s3_versions(s3, bucket, versions)
            previous = versions
    if batch(("Versions", "DeleteMarkers")):
        raise RuntimeError(f"S3 history remains after deletion bucket={bucket} prefix={prefix}")
    return deleted
