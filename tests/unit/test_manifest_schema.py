"""Manifest schema versioning: rebuild, never migrate.

The manifest is sliding-window state the next run re-derives from the source,
so an older database is deleted and recreated from `schema.sql` rather than
migrated in place -- `schema.sql` stays the single definition of the schema.
These tests pin the rebuild actually happening (old rows gone, new tables
present) and the one direction that must still refuse: a database written by a
newer build.
"""

from __future__ import annotations

import sqlite3

import pytest

from fii_docs_watcher.clock import parse_delivery, today
from fii_docs_watcher.manifest.db import SCHEMA_VERSION, connect
from fii_docs_watcher.manifest.repo import ManifestRepo


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def test_a_fresh_database_lands_on_the_current_version(tmp_path) -> None:
    connection = connect(tmp_path / "manifest.sqlite")
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert {"documents", "download_attempts", "sync_state", "listing_cursor"} <= _tables(
            connection
        )
    finally:
        connection.close()


def test_an_older_database_is_deleted_and_rebuilt(tmp_path, caplog) -> None:
    path = tmp_path / "manifest.sqlite"
    connection = connect(path)
    # Plant a row, then rewind the version stamp: to the next open this is a
    # database written by an older build, whatever its actual tables say.
    connection.execute(
        "INSERT INTO sync_state (fundosnet_id, last_error) VALUES (21348, 'old state')"
    )
    connection.execute("PRAGMA user_version = 2")
    connection.close()

    with caplog.at_level("WARNING"):
        connection = connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # The rebuild is real: the old row did not survive, the new table exists.
        assert connection.execute("SELECT COUNT(*) AS n FROM sync_state").fetchone()["n"] == 0
        assert "listing_cursor" in _tables(connection)
    finally:
        connection.close()
    assert any("rebuilding the manifest" in record.message for record in caplog.records)


def test_a_newer_database_refuses_to_be_opened_by_an_older_build(tmp_path) -> None:
    path = tmp_path / "manifest.sqlite"
    connection = connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(RuntimeError, match="newer than this build"):
        connect(path)


def test_known_identities_answers_for_a_whole_tie_group_in_one_query(tmp_path) -> None:
    # Global listing rows carry idFundo as null, so the per-entity lookup
    # cannot serve the monitor's tie-group dedup; this one can, batched.
    connection = connect(tmp_path / "manifest.sqlite")
    try:
        repo = ManifestRepo(connection)
        connection.execute(
            "INSERT INTO documents (document_id, version, fundosnet_id, delivery_date,"
            " delivery_at, local_state, seen_at) VALUES"
            " (100, 1, 77, '2026-08-27', '2026-08-27T09:30-03:00', 'available', 't'),"
            " (200, 2, 88, '2026-08-27', '2026-08-27T09:30-03:00', 'available', 't')"
        )
        assert repo.known_identities([(100, 1), (200, 1), (300, 1)]) == {(100, 1)}
        assert repo.known_identities([]) == set()
    finally:
        connection.close()


class TestListingCursor:
    def _instant(self, time: str):
        return parse_delivery(f"{today().strftime('%d/%m/%Y')} {time}")

    def test_the_cursor_round_trips_at_minute_resolution(self, tmp_path) -> None:
        connection = connect(tmp_path / "manifest.sqlite")
        try:
            repo = ManifestRepo(connection)
            assert repo.listing_cursor(1) is None
            newest = self._instant("14:05")
            repo.advance_listing_cursor(1, newest)
            assert repo.listing_cursor(1) == newest
        finally:
            connection.close()

    def test_the_cursor_never_moves_backwards(self, tmp_path) -> None:
        # A stale multi-page read finishing after a fresher one must not
        # reopen rows already accounted for.
        connection = connect(tmp_path / "manifest.sqlite")
        try:
            repo = ManifestRepo(connection)
            repo.advance_listing_cursor(1, self._instant("14:05"))
            repo.advance_listing_cursor(1, self._instant("13:00"))
            assert repo.listing_cursor(1) == self._instant("14:05")
            repo.advance_listing_cursor(1, self._instant("15:00"))
            assert repo.listing_cursor(1) == self._instant("15:00")
        finally:
            connection.close()

    def test_cursors_are_independent_per_fund_type(self, tmp_path) -> None:
        connection = connect(tmp_path / "manifest.sqlite")
        try:
            repo = ManifestRepo(connection)
            repo.advance_listing_cursor(1, self._instant("14:05"))
            repo.advance_listing_cursor(11, self._instant("09:00"))
            assert repo.listing_cursor(11) == self._instant("09:00")
            assert [row["fund_type"] for row in repo.all_listing_cursors()] == [1, 11]
        finally:
            connection.close()
