"""Storage-side operations used by the sync flow."""

from __future__ import annotations

import os

from rich import print


def move_to_filtered_out(file, config, ya_client, parent_dir, entry_point):
    """Move a Yandex Disk file into a filtered-out folder or delete it."""
    filtered_out_dir = config['yandex']['disk']['filtered_out']

    if parent_dir == 'void':
        print(f"[magenta]Removing file '{file.md5}'('{file.path}')[/magenta]")
        ya_client.remove(file.path, n_retries=5, retry_interval=30)
    else:
        old_path = file.path.removeprefix('disk:')
        rel_path = os.path.relpath(old_path, entry_point)
        new_path = os.path.join(filtered_out_dir, parent_dir, rel_path)
        print(f"[cyan]Moving file '{file.md5}' from '{old_path} to '{new_path}'[/cyan]")
        ya_client.create_folders(os.path.dirname(new_path))
        ya_client.move(file.path, new_path, n_retries=5, retry_interval=30, overwrite=True)
        ya_client.unpublish(new_path)


def remove_from_s3(md5s, s3client, config):
    """Remove S3 objects related to the provided MD5s.

    This avoids full-bucket scans:
    - delete deterministic keys directly (content/metadata/upstream metadata),
    - list by MD5 prefix only for buckets with non-deterministic key suffixes.
    """
    md5_values = sorted({str(md5).strip() for md5 in md5s if str(md5).strip()})
    if not md5_values:
        return

    buckets = config["yandex"]["cloud"]["bucket"]
    content_bucket = buckets["content"]
    content_chunks_bucket = buckets["content_chunks"]
    documents_bucket = buckets["document"]
    images_bucket = buckets["image"]
    upstream_bucket = buckets["upstream_metadata"]
    metadata_bucket = buckets["metadata"]

    exact_keys_by_bucket = {
        content_bucket: [f"{md5}.zip" for md5 in md5_values],
        metadata_bucket: [f"{md5}-meta.zip" for md5 in md5_values],
        upstream_bucket: [f"{md5}.zip" for md5 in md5_values],
    }
    for bucket, keys in exact_keys_by_bucket.items():
        _delete_keys(s3client, bucket, keys)

    for md5 in md5_values:
        _delete_by_prefix(s3client, content_chunks_bucket, f"{md5}/")
        _delete_by_prefix(s3client, documents_bucket, md5)
        _delete_by_prefix(s3client, images_bucket, md5)


def _delete_by_prefix(s3client, bucket, prefix):
    """List objects by prefix and delete them in batches."""
    paginator = s3client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if key:
                keys.append(key)
    _delete_keys(s3client, bucket, keys)


def _delete_keys(s3client, bucket, keys):
    """Delete keys in S3 using 1000-object batches."""
    if not keys:
        return
    print(f"Removing {len(keys)} objects from bucket '{bucket}'")
    for i in range(0, len(keys), 1000):
        batch = [{"Key": key} for key in keys[i:i + 1000]]
        s3client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": batch, "Quiet": True},
        )


def publish_file(client, path):
    """Publish a file on Yandex Disk and return its public keys."""
    _ = client.publish(path)
    resp = client.get_meta(path, fields=['public_key', 'public_url'])
    return resp['public_key'], resp['public_url']
