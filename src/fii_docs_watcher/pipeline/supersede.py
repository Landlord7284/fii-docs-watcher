"""Keeping only the live version when a document is re-filed.

Funds re-file. Sometimes the source keeps the document id and bumps `versao`;
often it publishes the correction under a **new id** that carries a higher
`versao`, so the archive ends up with two files for one publication:

    HGBS11_Relatorios_1295651_V01.pdf
    HGBS11_Relatorios_1295810_V02.pdf

The archive is a reading queue for people. Only the live version is worth
reading, and a corrected copy sitting next to the original is exactly the noise
the inbox index exists to avoid. So the older file is deleted and its row is
kept, marked with what replaced it.

**Stated deviation from the specification.** Section 1 and section 2.4 place the
correlation of a document with its re-filing *under a different id* explicitly
outside Pipeline A, and the manifest originally promised that a superseded file
stays on disk because that history is worth keeping. Both are reversed here, on
the user's instruction. The publication identity invariant is untouched:
`(document_id, version)` is still the dedupe key, and the row is never deleted --
only the file is, and the manifest can still answer what was published and what
replaced it.

Two rules decide what replaces what. Both are applied, and neither subsumes the
other:

    R1  same `document_id`, strictly higher `version`. Spec-conformant, and kept
        as its own rule because a re-filing may correct the *reference date*,
        which would move it out of R2's group.
    R2  same entity, category, type, species and reference date; strictly higher
        `version`. This is the deviation, and the rule that catches the case
        above.

R2's safety rests entirely on *strictly higher version*. Two genuinely distinct
documents from one fund on one day -- two Fatos Relevantes, say -- both arrive as
`V01` and therefore never correlate; a group only produces a supersession when
the source itself bumped the version, which is the re-filing signature. R2 also
requires a non-empty reference date, its strongest discriminator: without one the
key would collapse to little more than a category.

The work is split in two on purpose:

    detect  runs after discovery. It records the marks and cancels a loser that
            has not been downloaded yet, so the run never fetches a file it is
            about to delete.
    sweep   runs after fetching, and deletes a loser's file **only once the
            winner is `available`**. If the replacement failed to download, the
            original stays: the reader is never left with neither copy.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from ..clock import RetentionWindow
from ..config import Config
from ..manifest.db import transaction
from ..manifest.repo import LocalState, ManifestDocument, ManifestRepo
from . import purge

log = logging.getLogger(__name__)

# States a loser may be in when it has never reached the archive. Cancelling
# these costs nothing: there is no file, so there is nothing to lose.
PENDING_STATES = frozenset(
    {
        LocalState.DISCOVERED.value,
        LocalState.DOWNLOADING.value,
        LocalState.FAILED.value,
        LocalState.SKIPPED.value,
    }
)


@dataclass
class SupersedeReport:
    detected: int = 0
    pending_cancelled: int = 0
    files_removed: int = 0
    # Losers whose replacement is not on disk yet. Their file stays put.
    deferred: int = 0
    errors: list[str] = field(default_factory=list)


def _group_key(document: ManifestDocument) -> tuple[object, ...] | None:
    """R2's grouping key, or None when the document cannot take part.

    Every component is normalised the way the source needs it: `tipoDocumento`
    arrives with stray spaces and inconsistent case, and comparing those raw
    would split a group that is really one publication.
    """
    reference = (document.reference_date or "").strip()
    if not reference:
        return None
    return (
        document.fundosnet_id,
        (document.category or "").strip().casefold(),
        (document.doc_type or "").strip().casefold(),
        (document.species or "").strip().casefold(),
        reference,
        (document.reference_date_format or "").strip(),
    )


def correlate(
    documents: list[ManifestDocument],
) -> list[tuple[ManifestDocument, ManifestDocument]]:
    """Pair each superseded document with the publication that replaced it.

    Returns `(loser, winner)` pairs. Pure: it touches neither the database nor
    the filesystem, which is what makes the two rules cheap to pin down in tests.
    """
    groups: dict[object, list[ManifestDocument]] = defaultdict(list)
    for document in documents:
        # R1: every version of one id is a group by construction.
        groups[("id", document.document_id)].append(document)
        key = _group_key(document)
        if key is not None:
            groups[("key", key)].append(document)

    # A document can lose in both groups at once; the winner is the same either
    # way, so collapse on the loser's identity and keep the highest version seen.
    best: dict[tuple[int, int], ManifestDocument] = {}
    for members in groups.values():
        # Highest version wins; a tie goes to the higher id, which is the later
        # filing. Ties only happen in an R2 group holding two distinct documents,
        # and an arbitrary winner there would make the run non-deterministic.
        winner = max(members, key=lambda m: (m.version, m.document_id))
        for member in members:
            if member.version >= winner.version:
                continue
            current = best.get(member.identity)
            if current is None or winner.version > current.version:
                best[member.identity] = winner

    by_identity = {document.identity: document for document in documents}
    return [(by_identity[identity], winner) for identity, winner in best.items()]


def detect(repo: ManifestRepo, window: RetentionWindow) -> SupersedeReport:
    """Record supersessions and stop the download queue for the losers.

    Runs before fetching, so a document that has already been replaced is never
    requested from the source at all.
    """
    report = SupersedeReport()
    pairs = correlate(repo.correlatable_in_window(window.first, window.last))
    if not pairs:
        return report

    with transaction(repo.connection):
        for loser, winner in pairs:
            changed = repo.mark_superseded_by(loser.identity, winner.identity)
            if loser.superseded_at is None:
                report.detected += changed
                log.info(
                    "a re-filing replaced an earlier publication",
                    extra={
                        "document_id": loser.document_id,
                        "version": loser.version,
                        "replaced_by": f"{winner.document_id} v{winner.version}",
                        "fundosnet_id": loser.fundosnet_id,
                        "category": loser.category,
                        "reference_date": loser.reference_date,
                    },
                )
            elif changed:
                log.info(
                    "a newer re-filing replaced the previously recorded replacement",
                    extra={
                        "document_id": loser.document_id,
                        "version": loser.version,
                        "replaced_by": f"{winner.document_id} v{winner.version}",
                    },
                )
            if loser.local_state in PENDING_STATES:
                # Never downloaded, so there is no file and nothing to defer.
                repo.set_state(loser.document_id, loser.version, LocalState.SUPERSEDED)
                report.pending_cancelled += 1

    return report


def sweep(repo: ManifestRepo, config: Config, window: RetentionWindow) -> SupersedeReport:
    """Delete the file of every superseded document whose replacement landed.

    The guard is the whole point: a loser is only deleted once its winner is
    `available`. A failed or filtered replacement leaves the original in place,
    because one readable copy is better than none.
    """
    report = SupersedeReport()
    documents = repo.correlatable_in_window(window.first, window.last)
    by_identity = {document.identity: document for document in documents}

    doomed: list[ManifestDocument] = []
    for document in documents:
        if document.local_state != LocalState.AVAILABLE.value or not document.superseded_at:
            continue
        winner_identity = document.superseded_by
        winner = by_identity.get(winner_identity) if winner_identity else None
        if winner is None or winner.local_state != LocalState.AVAILABLE.value:
            report.deferred += 1
            log.info(
                "keeping a superseded file until its replacement is on disk",
                extra={
                    "document_id": document.document_id,
                    "version": document.version,
                    "replaced_by": str(winner_identity),
                },
            )
            continue
        doomed.append(document)

    if not doomed:
        return report

    removed = purge.remove_files(config, doomed, errors=report.errors)
    with transaction(repo.connection):
        repo.mark_superseded_removed(removed)
    report.files_removed = len(removed)

    removed_set = set(removed)
    for document in doomed:
        if document.identity not in removed_set:
            continue
        log.info(
            "removed a superseded file",
            extra={
                "document_id": document.document_id,
                "version": document.version,
                "path": document.path,
                "replaced_by": str(document.superseded_by),
            },
        )
    return report
