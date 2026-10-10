"""Google Slides fallback for byte-detected legacy PowerPoint sources."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from app.modules.library.google_doc_conversion import (
    _load_credentials,
)
from app.runtime_config import config_text


class GoogleDrivePptxConverter:
    """Import binary PowerPoint as Slides, export PPTX, and remove the upload."""

    def __init__(self) -> None:
        self._service: Any | None = None

    def _drive(self) -> Any:
        if self._service is None:
            self._service = build(
                "drive", "v3", credentials=_load_credentials(), cache_discovery=False
            )
        return self._service

    def __call__(self, source: Path, *, workspace: Path) -> Path:
        output_dir = workspace / "google-converted"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / "source.pptx"
        service = self._drive()
        uploaded = service.files().create(
            body={
                "name": "source.ppt",
                "mimeType": "application/vnd.google-apps.presentation",
                "parents": [
                    config_text("google", "conversion_folder_id")
                ],
            },
            media_body=MediaFileUpload(
                str(source), mimetype="application/vnd.ms-powerpoint", resumable=True
            ),
            fields="id",
        ).execute()
        file_id = str(uploaded.get("id") or "").strip()
        if not file_id:
            raise RuntimeError("Google Drive conversion upload returned no file id")
        try:
            request = service.files().export_media(
                fileId=file_id,
                mimeType=(
                    "application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation"
                ),
            )
            with output.open("wb") as handle:
                downloader = MediaIoBaseDownload(handle, request)
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
        finally:
            service.files().delete(fileId=file_id).execute()
        return output


__all__ = ["GoogleDrivePptxConverter"]
