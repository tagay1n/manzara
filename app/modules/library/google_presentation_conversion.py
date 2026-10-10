"""Google Slides fallback for byte-detected legacy PowerPoint sources."""

from pathlib import Path

from app.modules.library.google_drive_conversion import GoogleDriveConversionClient


class GoogleDrivePptxConverter:
    """Import binary PowerPoint as Slides, export PPTX, and remove the upload."""

    def __init__(self) -> None:
        self._client = GoogleDriveConversionClient()

    def __call__(self, source: Path, *, workspace: Path) -> Path:
        return self._client.convert(
            source, output=workspace / "google-converted" / "source.pptx",
            source_mime="application/vnd.ms-powerpoint",
            target_mime="application/vnd.google-apps.presentation",
            export_mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            upload_name="source.ppt",
        )


__all__ = ["GoogleDrivePptxConverter"]
