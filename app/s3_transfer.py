"""Keep managed S3 transfers on the calling task worker."""

from boto3.s3.transfer import TransferConfig


def sequential_transfer_config() -> TransferConfig:
    # CRT ignores thread settings; select the standard manager explicitly.
    return TransferConfig(use_threads=False, preferred_transfer_client="classic")
