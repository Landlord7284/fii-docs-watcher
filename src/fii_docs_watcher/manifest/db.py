"""SQLite connection and migrations.

The manifest lives in the data root, which the configuration forces onto a
filesystem local to the process: SQLite's locking and durability over SMB or
NFS are unreliable, and this database is the only thing standing between a
crashed run and a corrupted archive.

Migrations are tracked with `PRAGMA user_version`, which needs no bookkeeping
table and cannot itself get out of step with the schema.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: Path) -> sqlite3.Connection:
    """Open the manifest, applying pragmas and migrations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    connection.row_factory = sqlite3.Row

    # WAL lets a reader work while a writer commits, and survives a crash better
    # than the rollback journal.
    connection.execute("PRAGMA journal_mode = WAL")
    # NORMAL is the right trade here: a power loss can cost the last transaction,
    # and reconciliation is designed to recover exactly that.
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")

    _migrate(connection)
    return connection


def _migrate(connection: sqlite3.Connection) -> None:
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if current == SCHEMA_VERSION:
        return
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"manifest schema version {current} is newer than this build understands "
            f"({SCHEMA_VERSION}); upgrade fii-docs-watcher rather than downgrading the database"
        )

    if current == 0:
        connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        log.info("manifest schema created", extra={"version": SCHEMA_VERSION})

    # Future migrations chain from here, each guarded by the version it upgrades
    # from, so a database can be brought forward across several releases at once.

    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside one transaction, committing or rolling back as a unit.

    Discovery relies on this: documents and the watermark advance together, so a
    crash can never leave a watermark claiming progress for documents that were
    not recorded. Pages are collected in memory first, so no HTTP request ever
    happens with a transaction open.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
