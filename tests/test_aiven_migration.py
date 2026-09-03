from __future__ import annotations

from pathlib import Path

import scripts.migrate_postgres_to_aiven as migration
from scripts.migrate_postgres_to_aiven import (
    filter_restore_toc,
    normalize_postgres_url,
    render_service_entry,
    restore_dump,
)


def test_normalize_postgres_url_accepts_aiven_and_sqlalchemy_schemes() -> None:
    assert normalize_postgres_url("postgres://user:pw@host/db") == (
        "postgresql://user:pw@host/db"
    )
    assert normalize_postgres_url("postgresql+psycopg2://user:pw@host/db") == (
        "postgresql://user:pw@host/db"
    )


def test_service_entry_keeps_secret_out_of_process_arguments() -> None:
    entry = render_service_entry(
        "target",
        "postgres://avnadmin:secret@db.example:12826/defaultdb"
        "?sslmode=verify-full&sslrootcert=%2Ftmp%2Fca.pem",
    )

    assert "[target]" in entry
    assert "password=secret" in entry
    assert "sslmode=verify-full" in entry
    assert "sslrootcert=/tmp/ca.pem" in entry


def test_restore_toc_keeps_public_objects_but_not_public_schema_creation() -> None:
    toc = """;
1; 2615 2200 SCHEMA - public owner
2; 1259 10 TABLE public document owner
3; 1259 11 TABLE monocorpus runs owner
"""

    filtered = filter_restore_toc(toc)

    assert ";1; 2615 2200 SCHEMA - public owner" in filtered
    assert "2; 1259 10 TABLE public document owner" in filtered
    assert "3; 1259 11 TABLE monocorpus runs owner" in filtered


def test_restore_dump_uses_single_worker_phases(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command, service_file, **kwargs):
        commands.append(command)

    monkeypatch.setattr(migration, "_run", fake_run)

    restore_dump(Path("service.conf"), Path("dump"), Path("restore.list"))

    assert [
        next(value for value in command if value.startswith("--section="))
        for command in commands
    ] == ["--section=pre-data", "--section=data", "--section=post-data"]
    assert all("--jobs=1" in command for command in commands)
