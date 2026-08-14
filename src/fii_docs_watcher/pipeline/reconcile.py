"""Startup reconciliation: healing whatever the last run left half-done.

The download protocol has a window between renaming a file into place and
committing that fact to the manifest. A crash inside that window leaves a valid
document on disk that the manifest still calls `downloading` -- and because
idempotency is by manifest, nothing would ever look at it again.

So every run begins by settling each intermediate record:

    the destination exists and re-validates  -> promote to `available`, never demote
    the destination is missing               -> put it back in the download queue
    an orphaned `.part` older than a threshold -> delete it

Promotion re-reads and re-validates the bytes rather than trusting the path,
because "a file exists at this path" is exactly the evidence this pipeline is
built not to rely on. When a hash was already recorded and the file's hash
differs, that is reported: it means something rewrote the archive.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..fnet.download import validate_file
from ..manifest.repo import INTERMEDIATE_STATES, LocalState, ManifestRepo
from . import naming

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    promoted: int = 0
    requeued: int = 0
    parts_removed: int = 0
    hash_mismatches: list[str] = field(default_factory=list)


def _resolve(config: Config, relative: str | None) -> Path | None:
    if not relative:
        return None
    return config.paths.documents_root / relative


def run(repo: ManifestRepo, config: Config) -> ReconcileReport:
    """Settle every intermediate record, then sweep stale staging files."""
    report = ReconcileReport()

    for document in repo.in_state(INTERMEDIATE_STATES):
        path = _resolve(config, document.path)

        if path is not None and path.is_file():
            try:
                content = path.read_bytes()
            except OSError as exc:
                log.error(
                    "could not read a file left by a previous run; requeueing the download",
                    extra={"document_id": document.document_id, "error": str(exc)},
                )
                repo.set_state(document.document_id, document.version, LocalState.DISCOVERED)
                report.requeued += 1
                continue

            content_hash = validate_file(
                content, document_id=document.document_id, version=document.version
            )
            if content_hash is not None:
                if document.content_hash and document.content_hash != content_hash:
                    message = (
                        f"document {document.document_id} v{document.version} has hash "
                        f"{content_hash[:12]} on disk but {document.content_hash[:12]} in the "
                        "manifest; the archived file was modified after it was written"
                    )
                    report.hash_mismatches.append(message)
                    log.error(
                        "archived file does not match its recorded hash",
                        extra={
                            "document_id": document.document_id,
                            "expected": document.content_hash,
                            "found": content_hash,
                        },
                    )
                repo.mark_available(
                    document.document_id,
                    document.version,
                    path=document.path or str(path.name),
                    extension=document.extension or path.suffix.lstrip("."),
                    content_hash=content_hash,
                )
                report.promoted += 1
                log.info(
                    "consolidated a document left behind by an interrupted run",
                    extra={"document_id": document.document_id, "version": document.version},
                )
                continue

            log.warning(
                "a file from a previous run failed validation; downloading it again",
                extra={"document_id": document.document_id, "path": str(path)},
            )

        # No destination, or one that no longer validates: back to the queue.
        repo.set_state(document.document_id, document.version, LocalState.DISCOVERED)
        report.requeued += 1

    report.parts_removed = sweep_staging(config)

    if report.promoted or report.requeued or report.parts_removed:
        log.info(
            "reconciliation finished",
            extra={
                "promoted": report.promoted,
                "requeued": report.requeued,
                "parts_removed": report.parts_removed,
            },
        )
    return report


def sweep_staging(config: Config) -> int:
    """Delete staging files older than the configured threshold.

    Only old ones: a `.part` written seconds ago may belong to a run that is
    still going, and this must stay safe to call even when the lock has just
    been reclaimed from a process that had not fully exited.
    """
    tmp_dir = config.paths.tmp_dir
    if not tmp_dir.is_dir():
        return 0

    cutoff = time.time() - config.download.stale_part_hours * 3600
    removed = 0
    for entry in tmp_dir.iterdir():
        if not entry.is_file() or entry.suffix != ".part":
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
            removed += 1
            log.debug("removed an orphaned staging file", extra={"file": entry.name})
        except OSError as exc:  # pragma: no cover
            log.warning(
                "could not remove a staging file", extra={"file": entry.name, "error": str(exc)}
            )
    return removed


def expected_part_path(config: Config, document_id: int, version: int) -> Path:
    """Where a download in flight for this document would be staged."""
    return config.paths.tmp_dir / naming.part_filename(document_id, version)
