"""Manifest migrations.

`PRAGMA user_version` is the only bookkeeping, so a migration that fails to
apply leaves a database that reports the new version while missing the columns.
These tests exist to make that impossible to ship unnoticed.
"""

from __future__ import annotations

import sqlite3

import pytest

from fii_docs_watcher.manifest.db import SCHEMA_VERSION, connect


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in connection.execute("PRAGMA table_info(documents)")}


def test_a_fresh_database_lands_on_the_current_version(tmp_path) -> None:
    connection = connect(tmp_path / "manifest.sqlite")
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert {"superseded_at", "superseded_by_id", "superseded_by_version"} <= _columns(
            connection
        )
    finally:
        connection.close()


def test_a_version_one_database_gains_the_supersession_columns(tmp_path) -> None:
    path = tmp_path / "manifest.sqlite"
    connection = connect(path)
    # Rewind to what version 1 looked like: the two columns did not exist.
    connection.execute("ALTER TABLE documents DROP COLUMN superseded_by_id")
    connection.execute("ALTER TABLE documents DROP COLUMN superseded_by_version")
    connection.execute("PRAGMA user_version = 1")
    connection.close()

    connection = connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert {"superseded_by_id", "superseded_by_version"} <= _columns(connection)
    finally:
        connection.close()


def test_a_newer_database_refuses_to_be_opened_by_an_older_build(tmp_path) -> None:
    path = tmp_path / "manifest.sqlite"
    connection = connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(RuntimeError, match="newer than this build"):
        connect(path)
