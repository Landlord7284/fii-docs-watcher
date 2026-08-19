"""Purge: deleting date directories past the retention frontier.

`N` is the number of dates kept *including today*, so the frontier is
`today - (N - 1)`. Purge, the discovery window and the inbox index all derive
from that one value -- if they disagreed by even a day, discovery would download
documents that purge deletes minutes later, forever.

Purge runs unconditionally: no gate, no consultation with Pipeline B, no
dependency on any external state. Pipeline B has its own lifecycle and downloads
its own copies; making A's retention wait on B's progress would couple two
pipelines that are deliberately independent.

Rows are marked rather than deleted. Knowing a document once existed costs
almost nothing and answers "was this ever published?" long after the file is
gone. It is the file that is temporary, not the record.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..clock import RetentionWindow, parse_dir_name, to_dir_name
from ..config import Config
from ..manifest.repo import ManifestDocument, ManifestRepo

log = logging.getLogger(__name__)

# Directories in the documents root that are ours and are not dated archives.
PROTECTED_NAMES = frozenset({".tmp", "_inbox"})


@dataclass
class PurgeReport:
    directories_removed: int = 0
    files_removed: int = 0
    rows_marked: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def remove_files(config: Config, documents: Iterable[ManifestDocument]) -> list[tuple[int, int]]:
    """Delete the archived file of each document, and return what was removed.

    Shared by the two callers that delete individual files rather than a whole
    date directory: `rm --delete-documents` and the supersession sweep. Each
    then applies its own manifest update, because "removed because nobody
    follows this fund" and "removed because a re-filing replaced it" are
    different facts and the archive should be able to tell them apart.

    A file that cannot be deleted is logged and left out of the returned list,
    so the manifest is never updated to claim a file is gone while it is not.
    """
    removed: list[tuple[int, int]] = []
    touched_dirs: set[Path] = set()
    for document in documents:
        if not document.path:
            continue
        path = config.paths.documents_root / document.path
        try:
            path.unlink(missing_ok=True)
            touched_dirs.add(path.parent)
            removed.append((document.document_id, document.version))
        except OSError as exc:
            log.error(
                "could not delete an archived file",
                extra={"path": str(path), "error": str(exc)},
            )

    # Leave no empty date directories behind, so the archive keeps reading as
    # "these are the days that have something in them".
    for directory in touched_dirs:
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:  # pragma: no cover - a racing writer is fine to ignore
            pass
    return removed


def run(repo: ManifestRepo, config: Config, window: RetentionWindow) -> PurgeReport:
    """Delete every dated directory before the frontier, and mark its rows purged."""
    report = PurgeReport()
    root = config.paths.documents_root
    if not root.is_dir():
        return report

    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name in PROTECTED_NAMES:
            continue

        entry_date = parse_dir_name(entry.name)
        if entry_date is None:
            # Not one of ours. A human may keep notes in the share, and deleting
            # an unrecognised directory would be well beyond this job's remit.
            report.skipped.append(entry.name)
            log.debug("ignoring a directory that is not a date", extra={"dir": entry.name})
            continue

        if entry_date >= window.first:
            continue

        try:
            file_count = sum(1 for path in entry.rglob("*") if path.is_file())
            shutil.rmtree(entry)
            report.directories_removed += 1
            report.files_removed += file_count
            log.info(
                "purged a date directory past the retention frontier",
                extra={"dir": entry.name, "files": file_count},
            )
        except OSError as exc:
            report.errors.append(f"{entry.name}: {exc}")
            log.error(
                "could not remove a date directory",
                extra={"dir": entry.name, "error": str(exc)},
            )

    report.rows_marked = repo.mark_purged(window.first)

    # The inbox indexes follow the same retention as the documents they point at,
    # so that a stale index never links into a directory that no longer exists.
    _purge_inbox(config, window, report)

    if report.directories_removed or report.rows_marked:
        log.info(
            "purge finished",
            extra={
                "frontier": to_dir_name(window.first),
                "directories": report.directories_removed,
                "files": report.files_removed,
                "rows_marked": report.rows_marked,
            },
        )
    return report


def _purge_inbox(config: Config, window: RetentionWindow, report: PurgeReport) -> None:
    inbox = config.paths.inbox_dir
    if not inbox.is_dir():
        return
    for entry in sorted(inbox.iterdir()):
        if not entry.is_file() or entry.suffix != ".md":
            continue
        entry_date = parse_dir_name(entry.stem)
        if entry_date is None or entry_date >= window.first:
            continue
        try:
            entry.unlink()
            report.files_removed += 1
            log.debug("purged an inbox index", extra={"file": entry.name})
        except OSError as exc:  # pragma: no cover
            report.errors.append(f"_inbox/{entry.name}: {exc}")
