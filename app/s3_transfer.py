"""Keep managed S3 transfers on the calling task worker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from boto3 import Session
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from app.runtime_config import config_integer, config_text

if TYPE_CHECKING:
    from app.document_storage import S3ConnectionSettings


def create_s3_client(connection: S3ConnectionSettings, *, profile: str) -> Any:
    """Create a caller-owned client with the operation's network policy."""
    return Session().client(
        "s3", endpoint_url=connection.endpoint_url, region_name=connection.region_name,
        aws_access_key_id=connection.access_key_id,
        aws_secret_access_key=connection.secret_access_key,
        config=s3_client_config(profile),
    )


def s3_client_config(profile: str) -> Config:
    """Resolve one operation's explicit network policy."""
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        connect_timeout=config_integer(
            "network", "s3", profile, "connect_timeout_seconds"
        ),
        read_timeout=config_integer("network", "s3", profile, "read_timeout_seconds"),
        retries={
            "mode": config_text("network", "s3", profile, "retry_mode"),
            "total_max_attempts": config_integer(
                "network", "s3", profile, "total_attempts"
            ),
        },
    )


def sequential_transfer_config() -> TransferConfig:
    # CRT ignores thread settings; select the standard manager explicitly.
    return TransferConfig(
        use_threads=False,
        preferred_transfer_client="classic",
        multipart_threshold=config_integer(
            "documents", "transfer", "multipart_threshold_bytes"
        ),
        multipart_chunksize=config_integer(
            "documents", "transfer", "multipart_chunk_bytes"
        ),
        num_download_attempts=config_integer(
            "documents", "transfer", "download_attempts"
        ),
        max_io_queue=config_integer("documents", "transfer", "max_io_queue"),
        io_chunksize=config_integer("documents", "transfer", "io_chunk_bytes"),
    )
