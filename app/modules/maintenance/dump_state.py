"""Publish the document catalog to Google Sheets."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from sqlalchemy import Engine

from app.catalog.export import fetch_document_export, flatten_export_metadata
from app.modules.maintenance.catalog_sharing import validate_catalog_sharing


SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
SPREADSHEET_ID = "1qDm6iHJu44wN78YvYRbn44oFd28UfRs9HT-7UAzeZZ8"
WORKSHEET_NAME = "documents"
PREVIOUS_WORKSHEET_NAME = "tt"
SHEETS_WRITE_INTERVAL_SECONDS = 1.1

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
    "size",
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


def fetch_document_rows(engine: Engine, *, schema: str = "monocorpus") -> tuple[list[str], list[dict[str, Any]]]:
    """Read document rows and their normalized schema.org metadata."""
    rows = fetch_document_export(engine, schema=schema)
    return list(rows[0]) if rows else [*DOCUMENT_EXPORT_COLUMN_ORDER, "schema_org"], rows


def prepare_document_export(
    records: Iterable[Mapping[str, Any]],
    *,
    source_columns: Sequence[str] | None = None,
) -> tuple[list[str], list[list[Any]]]:
    """Flatten metadata and return an ordered, CSV-compatible export matrix."""
    rows = []
    for source in records:
        record = dict(source)
        schema_org = record.pop("schema_org", None)
        flattened = flatten_export_metadata(schema_org)
        for column in FLAT_METADATA_COLUMNS:
            record[column] = flattened.get(column)
        record["meta"] = (
            json.dumps(schema_org, ensure_ascii=False)
            if schema_org is not None
            else None
        )
        record["size"] = format_document_size(record.get("size"))
        rows.append([record.get(column) for column in DOCUMENT_EXPORT_COLUMN_ORDER])
    return list(DOCUMENT_EXPORT_COLUMN_ORDER), rows


def format_document_size(size: int | None) -> str | None:
    """Format persisted primary-storage bytes; an unknown size remains blank."""
    if size is None:
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("Document size must be a nonnegative integer or null")
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
    amount = float(size)
    unit = 0
    while amount >= 1024 and unit < len(units) - 1:
        amount /= 1024
        unit += 1
    return f"{size} B" if unit == 0 else f"{amount:.1f} {units[unit]}"


def write_csv(
    path: Path, columns: Sequence[str], rows: Iterable[Sequence[Any]]
) -> None:
    """Write one UTF-8 CSV using the supplied stable column order."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


def load_google_credentials(
    credentials_dir: Path, legacy_dir: Path | None = None
) -> Any:
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
    for row_number, row in enumerate(data, start=1):
        if any(len(value.encode("utf-16-le")) // 2 > 50000 for value in row):
            raise ValueError(f"Sheets row {row_number} exceeds the 50,000-character cell limit")
    required_rows = max(40000, len(data))
    required_columns = max(25, len(data[0]) if data else 1)

    write_started = False

    def pace_write() -> None:
        nonlocal write_started
        if write_started:
            time.sleep(SHEETS_WRITE_INTERVAL_SECONDS)
        write_started = True

    if should_stop():
        raise StopRequested("graceful stop requested before Sheets replacement")
    spreadsheet = gspread.authorize(credentials).open_by_key(SPREADSHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except WorksheetNotFound:
        try:
            worksheet = spreadsheet.worksheet(PREVIOUS_WORKSHEET_NAME)
        except WorksheetNotFound:
            pace_write()
            worksheet = spreadsheet.add_worksheet(
                title=WORKSHEET_NAME, rows=required_rows, cols=required_columns,
            )
        else:
            pace_write()
            worksheet.update_title(WORKSHEET_NAME)

    if should_stop():
        raise StopRequested("graceful stop requested before Sheets replacement")
    if worksheet.row_count < required_rows or worksheet.col_count < required_columns:
        pace_write()
        worksheet.resize(
            rows=max(worksheet.row_count, required_rows),
            cols=max(worksheet.col_count, required_columns),
        )
    pace_write()
    worksheet.clear()
    for start in range(0, len(data), chunk_size):
        chunk = data[start : start + chunk_size]
        pace_write()
        worksheet.update(values=chunk, range_name=f"A{start + 1}", value_input_option="RAW")
        print(
            f"dump state: sheets rows {start + 1}-{start + len(chunk)} uploaded",
            flush=True,
        )

    pace_write()
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
    schema: str = "monocorpus",
    validate_sharing: bool = False,
    should_stop: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Execute the complete export and return a compact run summary."""
    workspace.mkdir(parents=True, exist_ok=True)
    csv_path = workspace / "documents.csv"

    print("dump state: reading document catalog", flush=True)
    source_columns, records = fetch_document_rows(engine, schema=schema)
    if validate_sharing:
        validate_catalog_sharing(records)
        print(
            f"dump state: sharing validation passed for {len(records)} documents",
            flush=True,
        )
    columns, rows = prepare_document_export(records, source_columns=source_columns)
    write_csv(csv_path, columns, rows)
    print(
        f"dump state: exported rows={len(rows)} columns={len(columns)}",
        flush=True,
    )
    if should_stop():
        raise StopRequested("graceful stop requested before remote upload")

    credentials = load_google_credentials(credentials_dir, legacy_credentials_dir)
    print("dump state: publishing Google Sheets", flush=True)
    try:
        sheet_rows = upload_csv_to_sheets(csv_path, credentials)
    except Exception:
        _github_progress("- Sheets publishing failed.")
        raise
    _github_progress(f"- Sheets published: {sheet_rows} documents, {len(columns)} columns.")
    summary = {
        "spreadsheet_id": SPREADSHEET_ID,
        "worksheet": WORKSHEET_NAME,
        "rows_exported": len(rows),
        "rows_uploaded": sheet_rows,
        "columns_exported": len(columns),
        "sheet_columns": columns,
    }
    print(f"dump state: completed {json.dumps(summary, sort_keys=True)}", flush=True)
    return summary


def _github_progress(message: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")


__all__ = [
    "DOCUMENT_EXPORT_COLUMN_ORDER",
    "SPREADSHEET_ID",
    "StopRequested",
    "prepare_document_export",
    "run_dump",
    "write_csv",
]
