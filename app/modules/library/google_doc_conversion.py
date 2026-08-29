"""Google Drive fallback conversion for legacy word-processing documents."""

from __future__ import annotations

from pathlib import Path
import os
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from app.artifacts import flow_artifacts_dir


DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive",)
OPERATIVE_FOLDER_ID = "1WFYCcbrtKGv3KTwyKdcKHKxXwmr9iFHE"


def _load_credentials() -> Credentials:
    credentials_dir = flow_artifacts_dir("library") / "credentials"
    legacy_root = Path(
        os.environ.get("MONOCORPUS_REPO_PATH", "/home/tans1q/projects/monocorpus")
    ).expanduser()
    candidates = (
        credentials_dir / "personal_token.json",
        legacy_root / "_artifacts" / "credentials" / "personal_token.json",
    )
    token_path = next((path for path in candidates if path.is_file()), None)
    if token_path is None:
        raise FileNotFoundError(
            "Google Drive OAuth token not found; expected "
            f"{candidates[0]} or {candidates[1]}"
        )
    return Credentials.from_authorized_user_file(str(token_path), DRIVE_SCOPES)


class GoogleDriveDocxConverter:
    """Import a legacy document into Google Drive, export DOCX, then delete it."""

    def __init__(self) -> None:
        self._service: Any | None = None

    def _drive(self) -> Any:
        if self._service is None:
            self._service = build(
                "drive",
                "v3",
                credentials=_load_credentials(),
                cache_discovery=False,
            )
        return self._service

    def __call__(
        self,
        source: Path,
        *,
        workspace: Path,
        detected_format: str,
    ) -> Path:
        service = self._drive()
        output_dir = workspace / "google-converted"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "source.docx"
        mime_type = {
            "doc": "application/msword",
            "rtf": "application/rtf",
        }.get(str(detected_format), "application/octet-stream")
        uploaded = service.files().create(
            body={
                "name": Path(source).name,
                "mimeType": "application/vnd.google-apps.document",
                "parents": [
                    str(
                        os.environ.get(
                            "GOOGLE_DRIVE_CONVERSION_FOLDER_ID",
                            OPERATIVE_FOLDER_ID,
                        )
                    ).strip()
                    or OPERATIVE_FOLDER_ID
                ],
            },
            media_body=MediaFileUpload(
                str(source), mimetype=mime_type, resumable=True
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
                    "wordprocessingml.document"
                ),
            )
            with output_path.open("wb") as handle:
                downloader = MediaIoBaseDownload(handle, request)
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
        finally:
            service.files().delete(fileId=file_id).execute()
        return output_path


__all__ = ["GoogleDriveDocxConverter"]
