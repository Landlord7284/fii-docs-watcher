"""Downloading: a recoverable state machine, because two systems cannot commit together.

The filesystem and SQLite do not form one transaction. If the process dies
between the rename and the commit there is a perfectly good file on disk that
the manifest does not know about -- and since idempotency is by manifest, it
would never be recognised. So the protocol is ordered so that every crash point
leaves a state the next run can reason about:

    1. record (id, version) as  discovered
    2. download to  {documents_root}/.tmp/{id}_V{version}.part
    3. validate the content and hash it
    4. rename to the final destination
    5. mark  available  with its path and hash

Crash between 4 and 5 and the file exists while the manifest still says
`downloading`; startup reconciliation finds it, re-validates it, and promotes
it. Crash before 4 and an orphaned `.part` is all that is left, which is
discarded. Idempotency never rests on file existence alone.

The staging directory sits inside the documents root on purpose: `rename` is
only atomic within one filesystem, and the documents root is frequently a
different mount from the private data root.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..clock import parse_dir_name, to_dir_name
from ..config import Config
from ..errors import (
    CnpjDivergenceError,
    ContentValidationError,
    TransientSourceError,
    WatcherError,
)
from ..fnet.client import FnetClient
from ..fnet.download import DownloadedDocument
from ..fnet.download import fetch as fetch_document
from ..fnet.schema import looks_structured
from ..manifest.repo import AttemptOutcome, LocalState, ManifestDocument, ManifestRepo
from ..scope.cnpj import format_masked, same
from ..scope.models import Scope
from . import naming

log = logging.getLogger(__name__)

# Give up on a document after this many recorded attempts. It stays `failed` in
# the manifest rather than being retried forever on every run.
MAX_ATTEMPTS_PER_DOCUMENT = 5


@dataclass
class FetchReport:
    downloaded: int = 0
    skipped: int = 0
    # Pending, but their fund is not in the current watch list, so they were
    # left untouched rather than fetched.
    deferred: int = 0
    failed: int = 0
    bytes_written: int = 0
    cnpj_divergences: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class EntityIndex:
    """Maps a Fundos.NET id back to the scope and entity it came from.

    Needed because the manifest deliberately stores the emitting entity rather
    than the scope: the scope-to-entities relation belongs in the YAML, and the
    manifest records only what was observed.
    """

    def __init__(self, scopes: list[Scope]) -> None:
        self._by_id: dict[int, tuple[Scope, object]] = {}
        for scope in scopes:
            for entity in scope.entities:
                self._by_id[entity.fundosnet_id] = (scope, entity)

    def get(self, fundosnet_id: int) -> tuple[Scope, object] | None:
        return self._by_id.get(fundosnet_id)


def _ensure_dir(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except OSError:  # pragma: no cover - the share may not permit chmod
        log.debug("could not set directory mode", extra={"dir": str(path)})


def _check_cnpj(
    scope: Scope,
    entity: object,
    document: ManifestDocument,
    served_cnpj: str | None,
    report: FetchReport,
) -> None:
    """Close the loop on a resolution that was made by matching text.

    The comparison is against the *queried entity's* CNPJ, never the scope's
    reference CNPJ: in a multiclass fund a class legitimately has its own CNPJ,
    and comparing it against the umbrella fund's would report a divergence that
    does not exist.

    Absence of a CNPJ is not a divergence -- parsing the served filename is
    best-effort and must never halt the pipeline.
    """
    if served_cnpj is None:
        return

    expected = getattr(entity, "normalized_cnpj", None)
    if same(served_cnpj, expected):
        if not getattr(entity, "cnpj_confirmed", False):
            entity.cnpj_confirmed = True  # type: ignore[attr-defined]
            log.info(
                "entity CNPJ confirmed by a downloaded document",
                extra={
                    "scope": scope.label,
                    "fundosnet_id": document.fundosnet_id,
                    "entity_cnpj": format_masked(expected),
                },
            )
        return

    # It may still belong to a sibling entity of the same scope, which is normal
    # in a multiclass fund and not a divergence.
    if scope.entity_for_cnpj(served_cnpj) is not None:
        log.info(
            "document was filed under a sibling entity of the same scope",
            extra={
                "scope": scope.label,
                "document_id": document.document_id,
                "served_cnpj": format_masked(served_cnpj),
            },
        )
        return

    message = (
        f"{scope.label}: document {document.document_id} was served with CNPJ "
        f"{format_masked(served_cnpj)}, which matches no entity of this scope "
        f"(expected {format_masked(expected)})"
    )
    report.cnpj_divergences.append(message)
    raise CnpjDivergenceError(message, context={"document_id": document.document_id})


def _record_failure(
    repo: ManifestRepo,
    document: ManifestDocument,
    report: FetchReport,
    *,
    outcome: AttemptOutcome,
    error: Exception | str,
    started: float | None = None,
    size: int | None = None,
) -> None:
    """Apply the common manifest, attempt-history and report failure updates."""
    duration_ms = int((time.monotonic() - started) * 1000) if started is not None else None
    repo.record_attempt(
        document.document_id,
        document.version,
        outcome=outcome,
        size=size,
        duration_ms=duration_ms,
        error=str(error),
    )
    repo.mark_failed(document.document_id, document.version)
    report.failed += 1
    report.errors.append(f"document {document.document_id}: {error}")


def _publish(
    repo: ManifestRepo,
    config: Config,
    document: ManifestDocument,
    downloaded: DownloadedDocument,
    scope: Scope | None,
    delivery: date,
    report: FetchReport,
    started: float,
) -> None:
    """Publish validated bytes atomically and commit their manifest state."""
    entity_cnpj = downloaded.served.cnpj or document.entity_cnpj
    prefix = naming.entity_prefix(
        ticker=getattr(scope, "ticker", None),
        fund_description=document.fund_description or "",
        cnpj=entity_cnpj,
    )
    filename = naming.document_filename(
        prefix=prefix,
        category=document.category or "",
        species=document.species or "",
        document_id=document.document_id,
        version=document.version,
        extension=downloaded.extension,
    )

    target_dir = config.paths.documents_root / to_dir_name(delivery)
    _ensure_dir(target_dir, config.files.directory_mode)
    _ensure_dir(config.paths.tmp_dir, config.files.directory_mode)

    part_path = config.paths.tmp_dir / naming.part_filename(document.document_id, document.version)
    final_path = target_dir / filename
    relative_path = str(final_path.relative_to(config.paths.documents_root))

    # Persist the intended destination before publication. If the process dies
    # after the rename, startup reconciliation can now locate and adopt the file.
    repo.set_download_target(
        document.document_id,
        document.version,
        path=relative_path,
        extension=downloaded.extension,
    )
    part_path.write_bytes(downloaded.content)
    with suppress(OSError):
        os.chmod(part_path, config.files.file_mode)
    part_path.replace(final_path)

    repo.mark_available(
        document.document_id,
        document.version,
        path=relative_path,
        extension=downloaded.extension,
        content_hash=downloaded.content_hash,
    )
    repo.record_attempt(
        document.document_id,
        document.version,
        outcome=AttemptOutcome.SUCCESS,
        size=downloaded.size,
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    report.downloaded += 1
    report.bytes_written += downloaded.size
    log.info(
        "document filed",
        extra={
            "document_id": document.document_id,
            "version": document.version,
            "path": relative_path,
            "bytes": downloaded.size,
        },
    )


def fetch_one(
    client: FnetClient,
    repo: ManifestRepo,
    config: Config,
    document: ManifestDocument,
    index: EntityIndex,
    report: FetchReport,
) -> bool:
    """Download and file one document. Returns True when it lands on disk."""
    delivery = parse_dir_name(document.delivery_date)
    if delivery is None:
        message = f"document has an unusable delivery date: {document.delivery_date!r}"
        _record_failure(
            repo,
            document,
            report,
            outcome=AttemptOutcome.ERROR,
            error=message,
        )
        log.error(
            "document has an unusable delivery date and cannot be filed",
            extra={"document_id": document.document_id, "value": document.delivery_date},
        )
        return False

    if repo.attempt_count(document.document_id, document.version) >= MAX_ATTEMPTS_PER_DOCUMENT:
        message = f"exhausted the {MAX_ATTEMPTS_PER_DOCUMENT}-attempt download budget"
        report.failed += 1
        report.errors.append(f"document {document.document_id}: {message}")
        log.error(
            "document has exhausted its attempt budget; leaving it failed",
            extra={"document_id": document.document_id, "version": document.version},
        )
        return False

    found = index.get(document.fundosnet_id)
    scope, entity = found if found is not None else (None, None)

    expect_structured = looks_structured(document.category, document.doc_type, document.species)

    repo.set_state(document.document_id, document.version, LocalState.DOWNLOADING)
    started = time.monotonic()

    try:
        downloaded = fetch_document(
            client,
            document_id=document.document_id,
            version=document.version,
            expect_structured=expect_structured,
        )
    except ContentValidationError as exc:
        _record_failure(
            repo,
            document,
            report,
            outcome=AttemptOutcome.INVALID_CONTENT,
            error=str(exc),
            started=started,
        )
        log.error(
            "downloaded content failed validation and was not written",
            extra={"document_id": document.document_id, "error": str(exc)},
        )
        return False
    except (TransientSourceError, WatcherError) as exc:
        _record_failure(
            repo,
            document,
            report,
            outcome=AttemptOutcome.TRANSIENT,
            error=str(exc),
            started=started,
        )
        log.warning(
            "download failed; it will be retried on a later run",
            extra={"document_id": document.document_id, "error": str(exc)},
        )
        return False

    if scope is not None and entity is not None:
        try:
            _check_cnpj(scope, entity, document, downloaded.served.cnpj, report)
        except CnpjDivergenceError as exc:
            _record_failure(
                repo,
                document,
                report,
                outcome=AttemptOutcome.ERROR,
                size=downloaded.size,
                error=str(exc),
                started=started,
            )
            log.critical(
                "CNPJ divergence: the document was not filed and the resolution is not confirmed",
                extra={"document_id": document.document_id, "error": str(exc)},
            )
            return False

    # The signature has now had the final say, and it disagrees with the routing
    # hint: this document is not a format we keep. Decline it rather than file
    # it. Worth a warning, because a mispredict means the listing's category text
    # no longer implies the payload the way it did when that was measured.
    if not config.download.wants(downloaded.extension):
        repo.record_attempt(
            document.document_id,
            document.version,
            outcome=AttemptOutcome.FILTERED,
            size=downloaded.size,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"content is {downloaded.extension}, which is not in configured formats",
        )
        repo.set_state(document.document_id, document.version, LocalState.SKIPPED)
        report.skipped += 1
        log.warning(
            "downloaded document is not a configured format; it was not archived "
            "(the pre-download routing hint mispredicted it)",
            extra={
                "document_id": document.document_id,
                "expected_structured": expect_structured,
                "actual": downloaded.extension,
                "formats": ",".join(config.download.formats),
            },
        )
        return False

    try:
        _publish(repo, config, document, downloaded, scope, delivery, report, started)
    except OSError as exc:
        _record_failure(
            repo,
            document,
            report,
            outcome=AttemptOutcome.ERROR,
            error=exc,
            started=started,
            size=downloaded.size,
        )
        log.error(
            "could not write a document to the archive",
            extra={"document_id": document.document_id, "error": str(exc)},
        )
        return False
    return True


def _partition_by_entity(
    pending: list[ManifestDocument], index: EntityIndex
) -> tuple[list[ManifestDocument], list[ManifestDocument]]:
    """Split pending documents into those with a known owning entity and the rest."""
    known: list[ManifestDocument] = []
    orphaned: list[ManifestDocument] = []
    for document in pending:
        (known if index.get(document.fundosnet_id) is not None else orphaned).append(document)
    return known, orphaned


def _partition_by_format(
    pending: list[ManifestDocument], config: Config
) -> tuple[list[ManifestDocument], list[ManifestDocument]]:
    """Split pending documents into those worth fetching and those to decline.

    The prediction is the same text heuristic the listing offers, so this costs
    nothing and runs on every document every time -- which is what lets a
    previously skipped document be picked up as soon as the configured formats
    widen, with no re-discovery.

    A document already on disk keeps its recorded extension, so a format that
    stopped being wanted does not cause a re-download to confirm what it is.
    """
    if config.download.all_formats:
        return list(pending), []

    wanted: list[ManifestDocument] = []
    declined: list[ManifestDocument] = []
    for document in pending:
        if document.extension:
            predicted = document.extension
        else:
            predicted = (
                "xml"
                if looks_structured(document.category, document.doc_type, document.species)
                else "pdf"
            )
        (wanted if config.download.wants(predicted) else declined).append(document)
    return wanted, declined


def run(
    client: FnetClient,
    repo: ManifestRepo,
    config: Config,
    scopes: list[Scope],
    *,
    should_stop: object = None,
) -> FetchReport:
    """Download every document still pending. Isolated failures never stop the batch."""
    report = FetchReport()
    index = EntityIndex(scopes)

    # A document whose entity is not among the scopes handed to this run is
    # deferred, not fetched. Two reasons, and the second is the important one:
    # its fund may simply have left funds.yaml, and downloading for a fund
    # nobody follows is pointless -- but more than that, `fetch_one` can only
    # run the section 3.3 CNPJ check when it knows the entity, so fetching
    # without one would archive a document with that check silently skipped.
    #
    # Deferring rather than abandoning is deliberate: `run` passes only the
    # scopes that resolved, so an entity can be missing merely because the CVM
    # registry was unreachable. Permanent removal is settled in `run.execute`,
    # which can tell the two apart.
    known, orphaned = _partition_by_entity(repo.pending_downloads(), index)
    if orphaned:
        report.deferred = len(orphaned)
        log.info(
            "documents deferred: their fund is not in the current watch list",
            extra={
                "deferred": len(orphaned),
                "entities": sorted({d.fundosnet_id for d in orphaned}),
            },
        )

    wanted, declined = _partition_by_format(known, config)

    # Declining happens before any request. Section 2.5 sanctions the
    # "Estruturado" text as an early routing hint for exactly this purpose:
    # deciding what is worth fetching before spending a request on it.
    for document in declined:
        if document.local_state != LocalState.SKIPPED:
            repo.set_state(document.document_id, document.version, LocalState.SKIPPED)
            log.info(
                "not a configured format; skipped without downloading",
                extra={
                    "document_id": document.document_id,
                    "category": document.category,
                    "formats": ",".join(config.download.formats),
                },
            )
        report.skipped += 1

    if not wanted:
        log.info(
            "nothing pending to download",
            extra={"skipped_by_format": report.skipped} if report.skipped else {},
        )
        return report

    log.info(
        "starting downloads",
        extra={"pending": len(wanted), "skipped_by_format": report.skipped},
    )
    for document in wanted:
        if callable(should_stop) and should_stop():
            log.warning(
                "stopping downloads early on request",
                extra={
                    "downloaded": report.downloaded,
                    "remaining": len(wanted) - report.downloaded,
                },
            )
            break
        fetch_one(client, repo, config, document, index, report)

    return report
