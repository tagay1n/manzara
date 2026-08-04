"""S3 client helpers for Library runtime storage boundaries."""

from boto3 import Session
from botocore.config import Config
import os
from rich import print

from app.document_storage import load_document_storage_settings
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
        endpoint_url=str(cfg['yandex']['cloud'].get('endpoint_url') or 'https://storage.yandexcloud.net'),
        region_name=str(cfg['yandex']['cloud'].get('region_name') or 'ru-central1'),
    )


def create_document_session(config=None):
    """Create the configured primary document-storage S3 client."""
    cfg = config or read_config()
    settings = load_document_storage_settings(cfg)
    return Session().client(
        service_name='s3',
        aws_access_key_id=settings.primary.access_key_id,
        aws_secret_access_key=settings.primary.secret_access_key,
        endpoint_url=settings.primary.endpoint_url,
        region_name=settings.primary.region_name,
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}),
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
