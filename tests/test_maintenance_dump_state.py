"""Focused tests for the Maintenance database-state export."""

from __future__ import annotations

import csv
import json

import gspread

from app.modules.maintenance.dump_state import (
    DOCUMENT_EXPORT_COLUMN_ORDER,
    SPREADSHEET_ID,
    prepare_document_export,
    upload_csv_to_sheets,
    write_csv,
)


def test_dump_state_targets_requested_sheet_and_omits_unwanted_columns() -> None:
    columns, rows = prepare_document_export(
        [
            {
                "md5": "abc",
                "mime_type": "application/pdf",
                "ya_path": "/book.pdf",
                "genre": "legacy genre",
                "primary_storage_size": 123,
                "primary_storage_etag": "etag",
                "primary_storage_verified_at": "2026-08-21T10:00:00Z",
                "schema_org": {
                    "name": "Book title",
                    "author": [{"name": "Author Name"}],
                    "genre": ["Fiction"],
                    "datePublished": "2024",
                },
            }
        ]
    )

    assert SPREADSHEET_ID == "1qDm6iHJu44wN78YvYRbn44oFd28UfRs9HT-7UAzeZZ8"
    assert columns[:3] == ["md5", "mime_type", "ya_path"]
    assert all(column in DOCUMENT_EXPORT_COLUMN_ORDER for column in columns)
    assert "genre" not in columns
    assert "primary_storage_size" not in columns
    assert "primary_storage_etag" not in columns
    assert "primary_storage_verified_at" not in columns
    assert rows[0][columns.index("title")] == "Book title"
    assert rows[0][columns.index("author")] == "Author Name"
    assert json.loads(rows[0][columns.index("meta")])["genre"] == ["Fiction"]


def test_write_csv_preserves_strings_and_header_order(tmp_path) -> None:
    path = tmp_path / "monocorpus_backup.csv"
    columns = ["md5", "isbn", "title"]
    rows = [["abc", "0012345", "Китап"]]

    write_csv(path, columns, rows)

    with path.open(encoding="utf-8", newline="") as handle:
        assert list(csv.reader(handle)) == [columns, rows[0]]


def test_sheets_upload_resizes_small_existing_worksheet(monkeypatch, tmp_path) -> None:
    path = tmp_path / "export.csv"
    write_csv(path, ["md5", "title"], [["abc", "Китап"]])

    class Worksheet:
        id = 7
        row_count = 10
        col_count = 2

        def __init__(self) -> None:
            self.resized = None
            self.cleared = False
            self.updates = []

        def resize(self, *, rows, cols):  # noqa: ANN001
            self.resized = (rows, cols)

        def clear(self) -> None:
            self.cleared = True

        def update(self, *, values, range_name):  # noqa: ANN001
            self.updates.append((range_name, values))

    worksheet = Worksheet()

    class Spreadsheet:
        def worksheet(self, title):  # noqa: ANN001
            assert title == "tt"
            return worksheet

        def batch_update(self, payload):  # noqa: ANN001
            assert payload["requests"][0]["repeatCell"]["range"] == {"sheetId": 7}

    class Client:
        def open_by_key(self, key):  # noqa: ANN001
            assert key == SPREADSHEET_ID
            return Spreadsheet()

    monkeypatch.setattr(gspread, "authorize", lambda _credentials: Client())

    uploaded = upload_csv_to_sheets(path, object())

    assert uploaded == 1
    assert worksheet.resized == (40000, 25)
    assert worksheet.cleared is True
    assert worksheet.updates == [("A1", [["md5", "title"], ["abc", "Китап"]])]
