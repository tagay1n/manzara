"""S3 helper functions for Yandex Cloud storage."""

from boto3 import Session
import os
from rich import print

from core.config import read_config


def create_session(config=None):
    """Create a boto3 S3 client using Yandex Cloud credentials."""
    cfg = config or read_config()
    aws_access_key_id, aws_secret_access_key = map(
        cfg['yandex']['cloud'].get,
        ['aws_access_key_id', 'aws_secret_access_key'],
    )
    return Session().client(
        service_name='s3',
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        endpoint_url='https://storage.yandexcloud.net',
    )


def upload_file(path, bucket, key, session, skip_if_exists=False):
    """Upload a local file to S3 unless it already exists."""
    if not (skip_if_exists and session.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1).get("Contents", [])):
        print(f"Uploading doc '{key}'")
        session.upload_file(path, bucket, key)
    else:
        print(f"Doc '{key}' already exists")
    return f"{session._endpoint.host}/{bucket}/{key}"


def download(bucket, download_dir, prefix=''):
    """Stream-download all objects under a prefix into a local directory."""
    s3 = create_session()
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            local_path = os.path.join(download_dir, os.path.relpath(key, prefix))
            if not os.path.exists(local_path):
                print(f"Downloading {key} to {local_path}")
                s3.download_file(bucket, key, local_path)
            yield local_path
