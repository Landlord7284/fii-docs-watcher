"""Discovery: one query per entity, over the whole retention window, every run.

There is no incremental interval. Every run asks each entity for
`[today - (N-1), today]`, which collapses four separate problems into one query:

    what is new                 new documents appear inside the window
    catching up after downtime  the window already covers every recoverable day;
                                anything older would have been purged anyway
    status changing later       the row is re-found each run and its mutable
                                fields refreshed, without disturbing the file
    pagination drift            a per-entity query fits in very few pages

Querying with `idFundo` is what makes routing deterministic. The listing returns
`cnpjFundo` and `idFundo` as null on every row -- even when filtering by
`idFundo` -- so a global scan can only route by matching `descricaoFundo` text,
which fails silently on renames, on name collisions and on classes that carry
their own names. Asking per entity means the answer is known from the question.

The watermark is a record of progress, not an input: losing it costs nothing,
because the next run scans the whole window regardless.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass, field

from ..clock import RetentionWindow
from ..errors import TransientSourceError, WatcherError
from ..fnet.client import FnetClient
from ..fnet.listing import scan
from ..manifest.db import transaction
from ..manifest.repo import ManifestRepo
from ..scope.models import Entity, Scope

log = logging.getLogger(__name__)


@dataclass
class DiscoveryReport:
    entities_scanned: int = 0
    entities_failed: int = 0
    documents_seen: int = 0
    documents_new: int = 0
    superseded: int = 0
    incomplete_scans: int = 0
    invalid_rows: int = 0
    errors: list[str] = field(default_factory=list)


def discover_entity(
    client: FnetClient,
    repo: ManifestRepo,
    *,
    scope: Scope,
    entity: Entity,
    window: RetentionWindow,
    page_length: int,
    report: DiscoveryReport,
) -> None:
    """Scan one entity's window and persist what it found.

    HTTP happens first and entirely; only then does a single transaction record
    the documents and, if the scan proved complete, advance the watermark. That
    ordering is deliberate: it satisfies "documents are durable before the
    watermark moves" without holding a database transaction open across network
    calls.
    """
    result = scan(
        client,
        first=window.first,
        last=window.last,
        fundosnet_id=entity.fundosnet_id,
        page_length=page_length,
        fund_type=entity.fnet_fund_type,
    )

    report.documents_seen += len(result.rows)
    report.invalid_rows += len(result.row_errors)
    for error in result.row_errors:
        log.error(
            "listing row failed validation and was skipped",
            extra={"scope": scope.label, "fundosnet_id": entity.fundosnet_id, "error": str(error)},
        )

    with transaction(repo.connection):
        new_count = 0
        for row in result.rows:
            if repo.upsert_discovered(
                row, fundosnet_id=entity.fundosnet_id, entity_cnpj=entity.normalized_cnpj
            ):
                new_count += 1
            if row.version > 1:
                # The listing stops returning earlier versions once a re-filing
                # lands, so this is the only chance to record that v1 is history.
                report.superseded += repo.mark_superseded(row.document_id, row.version)

        if result.complete:
            repo.advance_watermark(entity.fundosnet_id, window.last)
        else:
            report.incomplete_scans += 1
            repo.record_entity_error(
                entity.fundosnet_id,
                f"scan covered {len(result.rows)} of {result.records_filtered} records",
            )

    report.documents_new += new_count
    log.info(
        "entity scanned",
        extra={
            "scope": scope.label,
            "fundosnet_id": entity.fundosnet_id,
            "window": str(window),
            "found": len(result.rows),
            "new": new_count,
            "pages": result.pages,
            "complete": result.complete,
        },
    )


def run(
    client: FnetClient,
    repo: ManifestRepo,
    scopes: list[Scope],
    window: RetentionWindow,
    *,
    page_length: int,
    should_stop: object = None,
) -> DiscoveryReport:
    """Discover across every entity of every scope.

    A failing entity is recorded and skipped; the rest of the batch continues.
    One misconfigured fund must never cost a day of everyone else's documents.
    """
    report = DiscoveryReport()

    for scope in scopes:
        if not scope.resolved:
            log.error(
                "scope has no resolved entities and will be skipped", extra={"scope": scope.label}
            )
            continue

        for entity in scope.entities:
            if callable(should_stop) and should_stop():
                log.warning("stopping discovery early on request")
                return report
            try:
                discover_entity(
                    client,
                    repo,
                    scope=scope,
                    entity=entity,
                    window=window,
                    page_length=page_length,
                    report=report,
                )
                report.entities_scanned += 1
            except (TransientSourceError, WatcherError) as exc:
                report.entities_failed += 1
                message = f"{scope.label}/{entity.fundosnet_id}: {exc}"
                report.errors.append(message)
                log.log(
                    getattr(exc, "severity", logging.ERROR),
                    "entity scan failed; continuing with the rest",
                    extra={
                        "scope": scope.label,
                        "fundosnet_id": entity.fundosnet_id,
                        "error": str(exc),
                    },
                )
                repo.record_entity_error(entity.fundosnet_id, str(exc))

    return report


def check_watermarks(
    repo: ManifestRepo,
    window: RetentionWindow,
    monitored_ids: Collection[int] | None = None,
) -> list[str]:
    """Report entities whose last complete scan predates the retention frontier.

    Beyond that frontier documents were published and purged without ever being
    seen. Nothing can recover them, so this is a warning rather than a repair.

    `monitored_ids` limits it to funds somebody still follows. A fund removed on
    purpose would otherwise raise this every run forever, and a warning that
    always fires is a warning nobody reads when it finally matters.
    """
    warnings: list[str] = []
    for row in repo.stale_watermarks(window.first, monitored_ids):
        message = (
            f"entity {row['fundosnet_id']} last completed a scan through "
            f"{row['last_window_end']}, before the retention frontier "
            f"{window.first.isoformat()}; documents delivered in that gap were never seen "
            "and are no longer recoverable"
        )
        warnings.append(message)
        log.warning(
            "watermark gap exceeds the retention window",
            extra={
                "fundosnet_id": row["fundosnet_id"],
                "last_window_end": row["last_window_end"],
                "frontier": window.first.isoformat(),
            },
        )
    return warnings
