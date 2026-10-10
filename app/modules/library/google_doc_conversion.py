"""Google Drive fallback conversion for legacy word-processing documents."""

from pathlib import Path

from app.modules.library.google_drive_conversion import GoogleDriveConversionClient


class GoogleDriveDocxConverter:
    """Import a legacy document into Google Drive, export DOCX, then delete it."""

    def __init__(self) -> None:
        self._client = GoogleDriveConversionClient()

    def __call__(
        self, source: Path, *, workspace: Path, detected_format: str,
    ) -> Path:
        source_mime = {"doc": "application/msword", "rtf": "application/rtf"}.get(
            str(detected_format), "application/octet-stream"
        )
        return self._client.convert(
            source, output=workspace / "google-converted" / "source.docx",
            source_mime=source_mime, target_mime="application/vnd.google-apps.document",
            export_mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            upload_name=Path(source).name,
        )


__all__ = ["GoogleDriveDocxConverter"]
