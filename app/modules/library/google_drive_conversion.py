"""Shared Google Drive import/export lifecycle for document converters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from app.artifacts import private_credentials_dir
from app.runtime_config import config_text

DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive",)


def _load_credentials() -> Credentials:
    token_path = private_credentials_dir("google-drive") / "personal_token.json"
    if not token_path.is_file():
        raise FileNotFoundError(
            f"Google Drive OAuth token not found; expected {token_path}"
        )
    return Credentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)


class GoogleDriveConversionClient:
    """Lazily authenticate, convert one source, and delete its remote temporary."""

    def __init__(self) -> None:
        self._service: Any | None = None

    def _drive(self) -> Any:
        if self._service is None:
            self._service = build(
                "drive", "v3", credentials=_load_credentials(), cache_discovery=False
            )
        return self._service

    def convert(
        self, source: Path, *, output: Path, source_mime: str,
        target_mime: str, export_mime: str, upload_name: str,
    ) -> Path:
        service = self._drive()
        output.parent.mkdir(parents=True, exist_ok=True)
        uploaded = service.files().create(
            body={"name": upload_name, "mimeType": target_mime,
                  "parents": [config_text("google", "conversion_folder_id")]},
            media_body=MediaFileUpload(str(source), mimetype=source_mime, resumable=True),
            fields="id",
        ).execute()
        file_id = str(uploaded.get("id") or "").strip()
        if not file_id:
            raise RuntimeError("Google Drive conversion upload returned no file id")
        try:
            request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            with output.open("wb") as handle:
                downloader = MediaIoBaseDownload(handle, request)
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
        finally:
            service.files().delete(fileId=file_id).execute()
        return output
