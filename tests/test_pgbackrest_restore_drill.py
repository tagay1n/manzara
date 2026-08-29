"""Focused tests for the isolated pgBackRest restore drill helper."""

from __future__ import annotations

from scripts.pgbackrest_restore_drill import render_drill_postgresql_conf


def test_render_drill_postgresql_conf_uses_isolated_socket_and_port() -> None:
    rendered = render_drill_postgresql_conf(
        data_path="/var/lib/postgresql/18/restore-drill/data",
        socket_path="/tmp/manzara-restore-drill",
        port=55432,
    )

    assert "data_directory = '/var/lib/postgresql/18/restore-drill/data'" in rendered
    assert "port = 55432" in rendered
    assert "unix_socket_directories = '/tmp/manzara-restore-drill'" in rendered
    assert "listen_addresses = ''" in rendered
    assert "archive_mode = off" in rendered
