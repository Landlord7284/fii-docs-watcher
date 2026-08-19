"""Manifest operations.

The rules encoded here that are easy to get wrong:

- Rediscovering a document updates its mutable fields and **never** re-downloads
  it. Idempotency is by `(document_id, version)`, so an upsert must not reset
  `local_state`, `path`, `content_hash` or `downloaded_at`.
- A file's existence is never evidence on its own. The manifest plus startup
  reconciliation decide what is present; the filesystem alone cannot, because it
  and SQLite do not commit together.
- Purge marks rows rather than deleting them. Knowing a document existed is
  cheap and useful; only the file is temporary.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from ..clock import timestamp, to_dir_name
from ..fnet.schema import DocumentRow

log = logging.getLogger(__name__)


class LocalState(StrEnum):
    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    AVAILABLE = "available"
    FAILED = "failed"
    PURGED = "purged"
    # Deliberately not archived: the configured formats exclude this one. Not a
    # failure -- nothing went wrong -- so it is neither retried nor reported as
    # an error. Re-evaluated each run, so widening the configuration later picks
    # these up without a fresh discovery pass.
    SKIPPED = "skipped"
    # The fund this document belongs to is no longer monitored. Discovered, but
    # never fetched and never retried, because nobody is following it any more.
    # Kept rather than deleted: it is still a true record of what the source
    # published while the fund was being watched.
    ABANDONED = "abandoned"
    # A corrected re-filing replaced this publication, so its file was deleted
    # while the replacement stays. Distinct from `purged` on purpose: `purged`
    # has to keep meaning "aged past the retention frontier" and nothing else,
    # or the archive can no longer explain why a file is gone.
    SUPERSEDED = "superseded"


class AttemptOutcome(StrEnum):
    SUCCESS = "success"
    TRANSIENT = "transient"
    INVALID_CONTENT = "invalid_content"
    ERROR = "error"
    # Downloaded, then declined: the early routing hint mispredicted the format
    # and the real one is not configured. Worth recording because the request
    # was really made, and because a mispredict is worth being able to count.
    FILTERED = "filtered"


# States a run may find on startup that mean work was interrupted mid-flight.
INTERMEDIATE_STATES = (LocalState.DISCOVERED.value, LocalState.DOWNLOADING.value)


@dataclass(frozen=True)
class ManifestDocument:
    """A document row as stored. Mirrors the `documents` table."""

    document_id: int
    version: int
    fundosnet_id: int
    entity_cnpj: str | None
    fund_description: str | None
    category: str | None
    doc_type: str | None
    species: str | None
    reference_date: str | None
    reference_date_format: str | None
    delivery_date: str
    delivery_at: str
    modality: str | None
    status: str | None
    local_state: str
    path: str | None
    extension: str | None
    content_hash: str | None
    downloaded_at: str | None
    purged_at: str | None
    superseded_at: str | None
    superseded_by_id: int | None
    superseded_by_version: int | None
    seen_at: str

    @property
    def superseded_by(self) -> tuple[int, int] | None:
        """Identity of the publication that replaced this one, if any."""
        if self.superseded_by_id is None or self.superseded_by_version is None:
            return None
        return (self.superseded_by_id, self.superseded_by_version)

    @property
    def identity(self) -> tuple[int, int]:
        return (self.document_id, self.version)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ManifestDocument:
        # `.keys()` is required, not stylistic: iterating a sqlite3.Row yields
        # its values, so `for key in row` would index the row by its own contents.
        return cls(**{key: row[key] for key in row.keys()})  # noqa: SIM118


class ManifestRepo:
    """All manifest reads and writes. Holds no state beyond the connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ------------------------------------------------------------------ documents

    def upsert_discovered(
        self, row: DocumentRow, *, fundosnet_id: int, entity_cnpj: str | None
    ) -> bool:
        """Record a document seen in the listing. Returns True if it is new.

        On conflict only the mutable descriptive fields and `seen_at` are
        touched. Everything describing the local copy is deliberately left
        alone, so a document that is rediscovered every day for a week is
        downloaded exactly once.

        The single exception is `abandoned`, which is returned to `discovered`.
        Being seen again means the fund is on the watch list once more -- only a
        monitored entity is ever queried -- and a fund that was removed and then
        re-added would otherwise keep a permanently stranded backlog: discovery
        would find these rows every run while nothing ever downloaded them.
        """
        now = timestamp()
        cursor = self.connection.execute(
            """
            INSERT INTO documents (
                document_id, version, fundosnet_id, entity_cnpj, fund_description,
                category, doc_type, species, reference_date, reference_date_format,
                delivery_date, delivery_at, modality, status, local_state, seen_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (document_id, version) DO UPDATE SET
                fund_description = excluded.fund_description,
                category         = excluded.category,
                doc_type         = excluded.doc_type,
                species          = excluded.species,
                status           = excluded.status,
                modality         = excluded.modality,
                entity_cnpj      = COALESCE(documents.entity_cnpj, excluded.entity_cnpj),
                seen_at          = excluded.seen_at,
                local_state      = CASE
                                     WHEN documents.local_state = 'abandoned'
                                       AND documents.purged_at IS NULL
                                     THEN 'discovered'
                                     ELSE documents.local_state
                                   END
            """,
            (
                row.document_id,
                row.version,
                fundosnet_id,
                entity_cnpj,
                row.fund_description,
                row.category,
                row.doc_type,
                row.species,
                row.reference_date,
                row.reference_date_format,
                to_dir_name(row.delivery_date),
                row.delivery_at.isoformat(timespec="minutes"),
                row.modality,
                row.status,
                LocalState.DISCOVERED.value,
                now,
            ),
        )
        # rowcount is 1 for a fresh insert and 2 for an upsert that updated.
        return cursor.rowcount == 1

    def correlatable_in_window(self, first: date, last: date) -> list[ManifestDocument]:
        """Rows a re-filing may replace, or be, inside the retention window.

        `abandoned` is excluded: nobody follows that fund any more, so there is
        nothing to keep tidy and no file to delete. Already-purged rows are
        excluded because their file is gone for a different reason.
        """
        return [
            ManifestDocument.from_row(r)
            for r in self.connection.execute(
                """
                SELECT * FROM documents
                 WHERE purged_at IS NULL
                   AND local_state IN (?, ?, ?, ?, ?)
                   AND delivery_date BETWEEN ? AND ?
                 ORDER BY document_id, version
                """,
                (
                    LocalState.DISCOVERED.value,
                    LocalState.DOWNLOADING.value,
                    LocalState.FAILED.value,
                    LocalState.SKIPPED.value,
                    LocalState.AVAILABLE.value,
                    to_dir_name(first),
                    to_dir_name(last),
                ),
            )
        ]

    def mark_superseded_by(
        self, loser: tuple[int, int], winner: tuple[int, int]
    ) -> int:
        """Record that `winner` replaced `loser`. Does not touch the file.

        Deleting the file is a separate step that runs only once the winner is
        on disk, so this may safely run before anything has been downloaded.
        """
        cursor = self.connection.execute(
            """
            UPDATE documents
               SET superseded_at = ?, superseded_by_id = ?, superseded_by_version = ?
             WHERE document_id = ? AND version = ? AND superseded_at IS NULL
            """,
            (timestamp(), winner[0], winner[1], loser[0], loser[1]),
        )
        return cursor.rowcount

    def mark_superseded_removed(self, identities: Sequence[tuple[int, int]]) -> int:
        """Consolidate rows whose file was deleted because a re-filing replaced it.

        `purged_at` is deliberately left NULL: the row has not aged out, and
        leaving it clear keeps `mark_purged` free to sweep it at the frontier
        along with everything else from that day.
        """
        if not identities:
            return 0
        cursor = self.connection.executemany(
            """
            UPDATE documents
               SET local_state = ?, path = NULL
             WHERE document_id = ? AND version = ?
            """,
            [(LocalState.SUPERSEDED.value, doc_id, version) for doc_id, version in identities],
        )
        return cursor.rowcount

    def pending_downloads(self, limit: int | None = None) -> list[ManifestDocument]:
        """Documents that still need fetching, oldest delivery first.

        `failed` is included so a transient failure is retried on the next run;
        the attempts table is where the history of those retries lives.

        `skipped` is included too, so that widening `[download].formats` later
        picks up what an earlier run declined, with no need to re-discover it.
        Re-evaluating a still-unwanted document is a local test against its
        category text and costs no request, so this is cheap to do every run.
        """
        sql = """
            SELECT * FROM documents
             WHERE local_state IN (?, ?, ?, ?)
               AND purged_at IS NULL
             ORDER BY delivery_date, document_id, version
        """
        params: list[object] = [
            LocalState.DISCOVERED.value,
            LocalState.DOWNLOADING.value,
            LocalState.FAILED.value,
            LocalState.SKIPPED.value,
        ]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [ManifestDocument.from_row(r) for r in self.connection.execute(sql, params)]

    def in_state(self, states: Sequence[str]) -> list[ManifestDocument]:
        placeholders = ",".join("?" * len(states))
        return [
            ManifestDocument.from_row(r)
            for r in self.connection.execute(
                f"SELECT * FROM documents WHERE local_state IN ({placeholders})", tuple(states)
            )
        ]

    def get(self, document_id: int, version: int) -> ManifestDocument | None:
        row = self.connection.execute(
            "SELECT * FROM documents WHERE document_id = ? AND version = ?",
            (document_id, version),
        ).fetchone()
        return ManifestDocument.from_row(row) if row else None

    def set_state(self, document_id: int, version: int, state: LocalState) -> None:
        self.connection.execute(
            "UPDATE documents SET local_state = ? WHERE document_id = ? AND version = ?",
            (state.value, document_id, version),
        )

    def mark_available(
        self, document_id: int, version: int, *, path: str, extension: str, content_hash: str
    ) -> None:
        """Consolidate a document as present on disk. The last step of the download."""
        self.connection.execute(
            """
            UPDATE documents
               SET local_state = ?, path = ?, extension = ?, content_hash = ?,
                   downloaded_at = COALESCE(downloaded_at, ?), purged_at = NULL
             WHERE document_id = ? AND version = ?
            """,
            (
                LocalState.AVAILABLE.value,
                path,
                extension,
                content_hash,
                timestamp(),
                document_id,
                version,
            ),
        )

    def mark_failed(self, document_id: int, version: int) -> None:
        self.connection.execute(
            "UPDATE documents SET local_state = ? WHERE document_id = ? AND version = ?",
            (LocalState.FAILED.value, document_id, version),
        )

    def available_in_window(self, first: date, last: date) -> list[ManifestDocument]:
        return [
            ManifestDocument.from_row(r)
            for r in self.connection.execute(
                """
                SELECT * FROM documents
                 WHERE local_state = ?
                   AND purged_at IS NULL
                   AND delivery_date BETWEEN ? AND ?
                 ORDER BY delivery_date DESC, delivery_at DESC, document_id
                """,
                (LocalState.AVAILABLE.value, to_dir_name(first), to_dir_name(last)),
            )
        ]

    def downloaded_between(self, since: str, until: str) -> list[ManifestDocument]:
        """Documents whose local copy was written in `[since, until)`.

        This is what the inbox index is built from: after an offline stretch the
        new arrivals are scattered across past delivery dates, so "what showed up
        today" cannot be answered by looking at today's directory.

        `superseded` rows are returned alongside `available` ones so the index
        for the day a document arrived can still say what replaced it, instead
        of the entry silently disappearing when the index is regenerated.
        """
        return [
            ManifestDocument.from_row(r)
            for r in self.connection.execute(
                """
                SELECT * FROM documents
                 WHERE local_state IN (?, ?)
                   AND purged_at IS NULL
                   AND downloaded_at >= ?
                   AND downloaded_at < ?
                 ORDER BY delivery_date DESC, delivery_at DESC, document_id
                """,
                (LocalState.AVAILABLE.value, LocalState.SUPERSEDED.value, since, until),
            )
        ]

    def mark_purged(self, before: date) -> int:
        """Mark every document delivered before `before` as purged."""
        cursor = self.connection.execute(
            """
            UPDATE documents
               SET local_state = ?, purged_at = ?, path = NULL
             WHERE delivery_date < ? AND purged_at IS NULL
            """,
            (LocalState.PURGED.value, timestamp(), to_dir_name(before)),
        )
        return cursor.rowcount

    def abandon_pending(self, fundosnet_ids: Sequence[int]) -> int:
        """Stop the download queue for entities that are no longer monitored.

        Without this, removing a fund from the watch list would leave its
        already-discovered backlog in the queue, and the next run would happily
        download documents for a fund nobody is following any more -- `discover`
        stops asking about it, but `fetch` works from the manifest, not from the
        scope list.

        Only pending rows are touched. Anything already on disk stays available
        and ages out through the normal retention frontier.
        """
        if not fundosnet_ids:
            return 0
        entity_slots = ",".join("?" * len(fundosnet_ids))
        cursor = self.connection.execute(
            f"""
            UPDATE documents
               SET local_state = ?
             WHERE fundosnet_id IN ({entity_slots})
               AND local_state IN (?, ?, ?, ?)
               AND purged_at IS NULL
            """,
            (
                LocalState.ABANDONED.value,
                *fundosnet_ids,
                LocalState.DISCOVERED.value,
                LocalState.DOWNLOADING.value,
                LocalState.FAILED.value,
                LocalState.SKIPPED.value,
            ),
        )
        return cursor.rowcount

    def abandon_pending_outside(self, keep_ids: Collection[int]) -> int:
        """Stand down the queue for every entity that is no longer configured at all.

        The counterpart to `abandon_pending`, for funds that left `funds.yaml`
        by being edited out rather than through `rm`. The caller passes the
        entity ids of *every* configured scope, resolved or not, so an entity
        that merely failed to resolve this run keeps its backlog -- only one
        that belongs to no scope at all is treated as removed.

        Passing an empty set is meaningful: it says no fund is configured, so
        nothing pending has an owner.
        """
        keep = tuple(keep_ids)
        keep_clause = f"AND fundosnet_id NOT IN ({','.join('?' * len(keep))})" if keep else ""
        cursor = self.connection.execute(
            f"""
            UPDATE documents
               SET local_state = ?
             WHERE local_state IN (?, ?, ?, ?)
               AND purged_at IS NULL
               {keep_clause}
            """,
            (
                LocalState.ABANDONED.value,
                LocalState.DISCOVERED.value,
                LocalState.DOWNLOADING.value,
                LocalState.FAILED.value,
                LocalState.SKIPPED.value,
                *keep,
            ),
        )
        return cursor.rowcount

    def forget_entities(self, fundosnet_ids: Sequence[int]) -> int:
        """Drop the per-entity sync state for entities no longer monitored.

        Only `sync_state` -- the watermark and last error, which mean nothing
        once nobody follows the fund. The `documents` rows stay: those are the
        record of what the source published, and that remains true.
        """
        if not fundosnet_ids:
            return 0
        slots = ",".join("?" * len(fundosnet_ids))
        cursor = self.connection.execute(
            f"DELETE FROM sync_state WHERE fundosnet_id IN ({slots})", tuple(fundosnet_ids)
        )
        return cursor.rowcount

    def available_for_entities(self, fundosnet_ids: Sequence[int]) -> list[ManifestDocument]:
        """Documents of these entities that are currently on disk."""
        if not fundosnet_ids:
            return []
        slots = ",".join("?" * len(fundosnet_ids))
        return [
            ManifestDocument.from_row(row)
            for row in self.connection.execute(
                f"""
                SELECT * FROM documents
                 WHERE fundosnet_id IN ({slots})
                   AND local_state = ?
                   AND purged_at IS NULL
                 ORDER BY delivery_date, document_id
                """,
                (*fundosnet_ids, LocalState.AVAILABLE.value),
            )
        ]

    def mark_documents_purged(self, identities: Sequence[tuple[int, int]]) -> int:
        """Mark specific publications purged, for files removed outside the retention job."""
        if not identities:
            return 0
        now = timestamp()
        cursor = self.connection.executemany(
            """
            UPDATE documents
               SET local_state = ?, purged_at = ?, path = NULL
             WHERE document_id = ? AND version = ?
            """,
            [(LocalState.PURGED.value, now, doc_id, version) for doc_id, version in identities],
        )
        return cursor.rowcount

    def counts_by_state(self) -> dict[str, int]:
        return {
            row["local_state"]: row["n"]
            for row in self.connection.execute(
                "SELECT local_state, COUNT(*) AS n FROM documents GROUP BY local_state"
            )
        }

    def known_identities_for_entity(self, fundosnet_id: int) -> set[tuple[int, int]]:
        return {
            (row["document_id"], row["version"])
            for row in self.connection.execute(
                "SELECT document_id, version FROM documents WHERE fundosnet_id = ?",
                (fundosnet_id,),
            )
        }

    # ------------------------------------------------------------------- attempts

    def record_attempt(
        self,
        document_id: int,
        version: int,
        *,
        outcome: AttemptOutcome,
        http_status: int | None = None,
        size: int | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO download_attempts
                (document_id, version, attempted_at, outcome, http_status, bytes,
                 duration_ms, error)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                document_id,
                version,
                timestamp(),
                outcome.value,
                http_status,
                size,
                duration_ms,
                error,
            ),
        )

    def attempt_count(self, document_id: int, version: int) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM download_attempts WHERE document_id = ? AND version = ?",
            (document_id, version),
        ).fetchone()
        return int(row["n"])

    # ----------------------------------------------------------------- sync state

    def watermark(self, fundosnet_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM sync_state WHERE fundosnet_id = ?", (fundosnet_id,)
        ).fetchone()

    def advance_watermark(self, fundosnet_id: int, window_end: date) -> None:
        """Record a scan that completed successfully. Never called for a short scan."""
        self.connection.execute(
            """
            INSERT INTO sync_state (fundosnet_id, last_success_at, last_window_end,
                                    last_error, last_error_at)
            VALUES (?,?,?,NULL,NULL)
            ON CONFLICT (fundosnet_id) DO UPDATE SET
                last_success_at = excluded.last_success_at,
                last_window_end = excluded.last_window_end,
                last_error      = NULL,
                last_error_at   = NULL
            """,
            (fundosnet_id, timestamp(), to_dir_name(window_end)),
        )

    def record_entity_error(self, fundosnet_id: int, message: str) -> None:
        """Record a failure against one entity without disturbing its watermark."""
        self.connection.execute(
            """
            INSERT INTO sync_state (fundosnet_id, last_error, last_error_at)
            VALUES (?,?,?)
            ON CONFLICT (fundosnet_id) DO UPDATE SET
                last_error    = excluded.last_error,
                last_error_at = excluded.last_error_at
            """,
            (fundosnet_id, message[:2000], timestamp()),
        )

    def stale_watermarks(
        self, frontier: date, fundosnet_ids: Collection[int] | None = None
    ) -> list[sqlite3.Row]:
        """Entities whose last successful scan predates the retention frontier.

        Past that point documents were published and purged without ever being
        seen, and no future run can recover them -- so this is reported rather
        than repaired.

        `fundosnet_ids` restricts the answer to entities somebody still
        monitors. Without it, a fund removed on purpose would keep producing an
        unrecoverable-gap warning on every run for the rest of time, which is
        exactly how a warning that matters gets trained out of a reader.
        """
        params: list[object] = [to_dir_name(frontier)]
        clause = ""
        if fundosnet_ids is not None:
            ids = tuple(fundosnet_ids)
            if not ids:
                return []
            clause = f"AND fundosnet_id IN ({','.join('?' * len(ids))})"
            params.extend(ids)
        return list(
            self.connection.execute(
                f"""
                SELECT * FROM sync_state
                 WHERE last_window_end IS NOT NULL AND last_window_end < ?
                 {clause}
                 ORDER BY last_window_end
                """,
                params,
            )
        )

    def entity_document_counts(self) -> dict[int, int]:
        return {
            row["fundosnet_id"]: row["n"]
            for row in self.connection.execute(
                "SELECT fundosnet_id, COUNT(*) AS n FROM documents GROUP BY fundosnet_id"
            )
        }
