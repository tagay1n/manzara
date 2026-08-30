"""Export the document catalog to CSV, Google Drive, and Google Sheets."""

from __future__ import annotations

import csv
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
import zipfile

from sqlalchemy import Engine, text

from app.modules.library.runtime.metadata.fields import extract_flat_fields


SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)
SHARED_FOLDER_ID = "1WFYCcbrtKGv3KTwyKdcKHKxXwmr9iFHE"
SPREADSHEET_ID = "1qDm6iHJu44wN78YvYRbn44oFd28UfRs9HT-7UAzeZZ8"
WORKSHEET_NAME = "tt"

EXCLUDED_CSV_COLUMNS = {
    "ya_public_key",
    "ya_resource_id",
    "content_extraction_method",
    "meta_extraction_method",
    "lib",
    "genre",
    "primary_storage_size",
    "primary_storage_etag",
    "primary_storage_verified_at",
}
DOCUMENT_EXPORT_COLUMN_ORDER = [
    "md5",
    "mime_type",
    "ya_path",
    "ya_public_url",
    "publisher",
    "author",
    "title",
    "isbn",
    "publish_year",
    "language",
    "translated",
    "page_count",
    "full",
    "sharing_restricted",
    "document_url",
    "content_url",
    "meta",
]
FLAT_METADATA_COLUMNS = (
    "publisher",
    "author",
    "title",
    "isbn",
    "publish_year",
    "translated",
    "page_count",
)


class StopRequested(RuntimeError):
    """Raised when a graceful stop is observed at an export boundary."""


def fetch_document_rows(engine: Engine) -> tuple[list[str], list[dict[str, Any]]]:
    """Read document rows and their normalized schema.org metadata."""
    query = text(
        """
        SELECT d.*, m.schema_org
        FROM document AS d
        LEFT JOIN metadata AS m ON m.md5 = d.md5
        ORDER BY d.ya_path
        """
    )
    with engine.connect() as connection:
        result = connection.execute(query)
        source_columns = list(result.keys())
        rows = [dict(row) for row in result.mappings()]
    return source_columns, rows


def prepare_document_export(
    records: Iterable[Mapping[str, Any]],
    *,
    source_columns: Sequence[str] | None = None,
) -> tuple[list[str], list[list[Any]]]:
    """Flatten metadata and return an ordered, CSV-compatible export matrix."""
    record_list = [dict(record) for record in records]
    discovered_columns = list(source_columns or ())
    for record in record_list:
        for column in record:
            if column not in discovered_columns:
                discovered_columns.append(column)

    rows: list[dict[str, Any]] = []
    for record in record_list:
        has_schema_org = "schema_org" in record
        schema_org = record.pop("schema_org", None)
        if not has_schema_org and "meta" in record:
            schema_org = record.get("meta")
        for column in EXCLUDED_CSV_COLUMNS:
            record.pop(column, None)
        record.pop("meta", None)
        flattened = extract_flat_fields(schema_org)
        for column in FLAT_METADATA_COLUMNS:
            record[column] = flattened.get(column)
        record["meta"] = (
            json.dumps(schema_org, ensure_ascii=False) if schema_org is not None else None
        )
        rows.append(record)

    candidate_columns = [
        column
        for column in discovered_columns
        if column not in EXCLUDED_CSV_COLUMNS and column not in {"schema_org", "meta"}
    ]
    for column in (*FLAT_METADATA_COLUMNS, "meta"):
        if column not in candidate_columns:
            candidate_columns.append(column)
    preferred = [
        column for column in DOCUMENT_EXPORT_COLUMN_ORDER if column in candidate_columns
    ]
    remaining = [column for column in candidate_columns if column not in preferred]
    columns = preferred + remaining
    return columns, [[record.get(column) for column in columns] for record in rows]


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    """Write one UTF-8 CSV using the supplied stable column order."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


def create_zip(csv_path: Path, zip_path: Path, title: str) -> None:
    """Create a ZIP archive containing the timestamped CSV."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(csv_path, arcname=f"{title}.csv")


def load_google_credentials(credentials_dir: Path, legacy_dir: Path | None = None) -> Any:
    """Load OAuth credentials, falling back to the former monocorpus files."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = credentials_dir / "personal_token.json"
    legacy_token = legacy_dir / "personal_token.json" if legacy_dir else None
    existing_token = token_path if token_path.exists() else legacy_token
    if existing_token is not None and existing_token.exists():
        return Credentials.from_authorized_user_file(str(existing_token), SCOPES)

    client_secret = credentials_dir / "client_secret.json"
    if not client_secret.exists() and legacy_dir is not None:
        client_secret = legacy_dir / "client_secret.json"
    if not client_secret.exists():
        raise FileNotFoundError(
            f"Google OAuth client secret not found at {credentials_dir / 'client_secret.json'}"
        )

    credentials = InstalledAppFlow.from_client_secrets_file(
        str(client_secret), SCOPES
    ).run_local_server(port=0)
    credentials_dir.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def upload_zip_to_drive(zip_path: Path, credentials: Any, title: str) -> None:
    """Upload the state archive to the established shared Drive folder."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    service = build("drive", "v3", credentials=credentials)
    media = MediaFileUpload(str(zip_path), mimetype="application/zip", resumable=True)
    service.files().create(
        body={
            "name": f"{title}.zip",
            "mimeType": "application/zip",
            "parents": [SHARED_FOLDER_ID],
        },
        media_body=media,
        fields="id",
    ).execute()


def upload_csv_to_sheets(
    csv_path: Path,
    credentials: Any,
    *,
    chunk_size: int = 1000,
    should_stop: Callable[[], bool] = lambda: False,
) -> int:
    """Replace the target worksheet contents with raw CSV values in chunks."""
    import gspread
    from gspread.exceptions import WorksheetNotFound

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        data = list(csv.reader(handle))
    required_rows = max(40000, len(data))
    required_columns = max(25, len(data[0]) if data else 1)

    spreadsheet = gspread.authorize(credentials).open_by_key(SPREADSHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=WORKSHEET_NAME,
            rows=required_rows,
            cols=required_columns,
        )

    if should_stop():
        raise StopRequested("graceful stop requested before Sheets replacement")
    if worksheet.row_count < required_rows or worksheet.col_count < required_columns:
        worksheet.resize(
            rows=max(worksheet.row_count, required_rows),
            cols=max(worksheet.col_count, required_columns),
        )
    worksheet.clear()
    for start in range(0, len(data), chunk_size):
        chunk = data[start : start + chunk_size]
        worksheet.update(values=chunk, range_name=f"A{start + 1}")
        print(
            f"dump state: sheets rows {start + 1}-{start + len(chunk)} uploaded",
            flush=True,
        )

    spreadsheet.batch_update(
        {
            "requests": [
                {
                    "repeatCell": {
                        "range": {"sheetId": worksheet.id},
                        "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP"}},
                        "fields": "userEnteredFormat.wrapStrategy",
                    }
                }
            ]
        }
    )
    return max(0, len(data) - 1)


def run_dump(
    *,
    engine: Engine,
    workspace: Path,
    credentials_dir: Path,
    legacy_credentials_dir: Path | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Execute the complete export and return a compact run summary."""
    workspace.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    title = f"monocorpus_{timestamp}"
    csv_path = workspace / "monocorpus_backup.csv"
    zip_path = workspace / "monocorpus_backup.zip"

    print("dump state: reading document catalog", flush=True)
    source_columns, records = fetch_document_rows(engine)
    columns, rows = prepare_document_export(records, source_columns=source_columns)
    write_csv(csv_path, columns, rows)
    create_zip(csv_path, zip_path, title)
    print(
        f"dump state: exported rows={len(rows)} columns={len(columns)}",
        flush=True,
    )
    if should_stop():
        raise StopRequested("graceful stop requested before remote upload")

    credentials = load_google_credentials(credentials_dir, legacy_credentials_dir)
    upload_zip_to_drive(zip_path, credentials, title)
    print(f"dump state: Drive archive uploaded name={title}.zip", flush=True)
    # Once the remote publish starts, finish both destinations so Google Sheets is
    # never knowingly left cleared or only partly replaced.
    sheet_rows = upload_csv_to_sheets(csv_path, credentials)
    summary = {
        "spreadsheet_id": SPREADSHEET_ID,
        "worksheet": WORKSHEET_NAME,
        "rows_exported": len(rows),
        "rows_uploaded": sheet_rows,
        "columns_exported": len(columns),
        "drive_archive": f"{title}.zip",
    }
    print(f"dump state: completed {json.dumps(summary, sort_keys=True)}", flush=True)
    return summary


__all__ = [
    "DOCUMENT_EXPORT_COLUMN_ORDER",
    "SPREADSHEET_ID",
    "StopRequested",
    "prepare_document_export",
    "run_dump",
    "write_csv",
]
