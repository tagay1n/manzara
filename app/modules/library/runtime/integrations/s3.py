"""S3 client helpers for Library runtime storage boundaries."""

from boto3 import Session
from botocore.config import Config
from rich import print

from app.document_storage import load_document_storage_settings
from app.modules.runtime_shared_utils import read_config


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
