"""Sharing-policy checks must gate the exact catalog snapshot being exported."""

import pytest

from app.modules.maintenance import dump_state
from app.modules.maintenance.catalog_sharing import (
    RESTRICTED_PATH,
    SharingValidationError,
    validate_catalog_sharing,
)


def document(restricted=True, **changes):
    row = {
        "md5": "a" * 32,
        "ya_path": RESTRICTED_PATH + "/nested/book.pdf" if restricted else "/book.pdf",
        "sharing_restricted": restricted,
        "document_url": "enc:secret-token"
        if restricted
        else "https://example.org/book.pdf",
        "ya_public_url": None if restricted else "https://example.org/public",
    }
    return row | changes


@pytest.mark.parametrize("prefix", ["", "disk:"])
@pytest.mark.parametrize("empty", [None, "", " \t\n"])
def test_valid_restricted_and_public_rows(prefix, empty):
    restricted = document(
        ya_path=prefix + RESTRICTED_PATH + "/book.pdf", ya_public_url=empty
    )
    public = document(
        False,
        ya_path=prefix + RESTRICTED_PATH + "-other/book.pdf",
        document_url=None,
        ya_public_url=None,
    )
    validate_catalog_sharing([restricted, public])


@pytest.mark.parametrize("value", [None, "", " \t\n"])
def test_missing_restricted_document_link_is_allowed(value):
    validate_catalog_sharing([document(document_url=value)])


@pytest.mark.parametrize("restricted", [True])
@pytest.mark.parametrize("value", [None, 0, 1, "true", "false"])
def test_flag_must_be_an_actual_matching_boolean(restricted, value):
    with pytest.raises(SharingValidationError, match="sharing_restricted"):
        validate_catalog_sharing([document(restricted, sharing_restricted=value)])


@pytest.mark.parametrize(
    "row,field",
    [
        (document(sharing_restricted=False), "sharing_restricted"),
        (document(document_url="https://example.org/secret"), "document_url"),
        (document(document_url="enc:"), "document_url"),
        (document(ya_public_url="https://example.org/secret"), "ya_public_url"),
    ],
)
def test_invalid_rows_are_rejected(row, field):
    with pytest.raises(SharingValidationError, match=field):
        validate_catalog_sharing([row])


def test_folder_itself_is_restricted():
    validate_catalog_sharing([document(ya_path=RESTRICTED_PATH)])


def test_reports_all_rules_and_documents_without_links():
    rows = [
        document(
            sharing_restricted=False,
            document_url="https://secret.example",
            ya_public_url="https://secret.example",
        ),
        document(
            True,
            md5="b" * 32,
            document_url="https://secret.example",
            ya_public_url="https://secret.example",
        ),
    ]
    with pytest.raises(SharingValidationError) as exc:
        validate_catalog_sharing(rows)
    message = str(exc.value)
    assert "b" * 32 in message
    assert all(field in message for field in ["sharing_restricted", "document_url", "ya_public_url"])
    assert "secret.example" not in message and "secret-token" not in message


def test_invalid_snapshot_stops_before_csv_credentials_or_upload(monkeypatch, tmp_path):
    monkeypatch.setattr(
        dump_state,
        "fetch_document_rows",
        lambda _engine: ([], [document(document_url="https://secret.example")]),
    )

    def unexpected(*args, **kwargs):
        pytest.fail("Export side effect occurred before sharing validation")

    for name in [
        "write_csv",
        "load_google_credentials",
        "upload_zip_to_drive",
        "upload_csv_to_sheets",
    ]:
        monkeypatch.setattr(dump_state, name, unexpected)
    with pytest.raises(SharingValidationError):
        dump_state.run_dump(
            engine=object(),
            workspace=tmp_path,
            credentials_dir=tmp_path,
            validate_sharing=True,
        )


def test_validated_snapshot_is_the_snapshot_uploaded(monkeypatch, tmp_path):
    records = [document(), document(False)]
    calls = []
    monkeypatch.setattr(
        dump_state, "fetch_document_rows", lambda _engine: (list(records[0]), records)
    )
    original_validate = dump_state.validate_catalog_sharing

    def validate(snapshot):
        assert snapshot is records
        original_validate(snapshot)
        calls.append("validate")

    monkeypatch.setattr(dump_state, "validate_catalog_sharing", validate)
    monkeypatch.setattr(dump_state, "load_google_credentials", lambda *_args: object())

    def upload_drive(path, *_args):
        assert calls == ["validate"]
        assert path.exists()
        calls.append("drive")

    def upload_sheet(path, *_args):
        assert calls == ["validate", "drive"]
        with path.open(newline="") as handle:
            import csv

            uploaded = list(csv.DictReader(handle))
        assert [row["document_url"] for row in uploaded] == [
            row["document_url"] for row in records
        ]
        calls.append("sheets")
        return len(uploaded)

    monkeypatch.setattr(dump_state, "upload_zip_to_drive", upload_drive)
    monkeypatch.setattr(dump_state, "upload_csv_to_sheets", upload_sheet)
    summary = dump_state.run_dump(
        engine=object(),
        workspace=tmp_path,
        credentials_dir=tmp_path,
        validate_sharing=True,
    )
    assert calls == ["validate", "drive", "sheets"]
    assert summary["rows_uploaded"] == 2


def test_cli_enables_gate_and_returns_failure(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    from app.modules.maintenance.runtime import dump_state as cli

    monkeypatch.setattr("sys.argv", ["dump_state", "--validate-sharing"])
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://unused",
            database_schema="monocorpus",
            database_pool_size=1,
        ),
    )
    monkeypatch.setattr(cli, "get_postgres_engine", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli, "workspace_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(cli, "private_credentials_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(cli.signal, "signal", lambda *_args: None)

    def fail(**kwargs):
        assert kwargs["validate_sharing"] is True
        raise SharingValidationError("export blocked")

    monkeypatch.setattr(cli, "run_dump", fail)
    monkeypatch.setattr(
        cli,
        "emit_run_artifact",
        lambda *_args: pytest.fail("Failed export reported success"),
    )
    assert cli.main() == 1
    assert "export blocked" in capsys.readouterr().out
