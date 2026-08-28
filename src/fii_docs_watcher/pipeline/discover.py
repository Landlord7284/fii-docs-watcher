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

`run_monitor` is the frequent profile's variant and the one stated exception to
the global listing being detective-only: one newest-first read per fund type
*gates* which of these per-entity queries the firing spends -- it never routes
a document. A row that matches a monitored name (exact, folded equality, within
its own fund type) triggers the normal per-entity query above; a row the gate
misses costs latency only, because the daily sweep still queries every entity
unconditionally. The cursor is what makes the firing cost proportional to the
publication rate instead of the watch list.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass, field

from ..clock import RetentionWindow
from ..errors import TransientSourceError, WatcherError
from ..fnet.client import FnetClient
from ..fnet.listing import scan, scan_newest
from ..manifest.db import transaction
from ..manifest.repo import ManifestRepo
from ..scope.models import Entity, Scope
from ..scope.resolver import normalize_name
from . import watchlist

log = logging.getLogger(__name__)


@dataclass
class DiscoveryReport:
    entities_scanned: int = 0
    entities_failed: int = 0
    documents_seen: int = 0
    documents_new: int = 0
    incomplete_scans: int = 0
    invalid_rows: int = 0
    # Entities whose stored fnet_fund_description was refreshed from a scanned
    # row. The caller persists funds.yaml when this is non-zero: the monitor's
    # gate matches against the stored spelling, so a rename the sweep observed
    # but never wrote back would blind the gate until someone re-resolved.
    descriptions_refreshed: int = 0
    # Newest-first reads that failed or broke the descending contract. Only
    # run_monitor produces these; each one already means the sweep is the only
    # thing covering that fund type until a later firing succeeds.
    listing_read_failures: int = 0
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
    covers_retention: bool = True,
) -> None:
    """Scan one entity's window and persist what it found.

    HTTP happens first and entirely; only then does a single transaction record
    the documents and, if the scan proved complete, advance the watermark. That
    ordering is deliberate: it satisfies "documents are durable before the
    watermark moves" without holding a database transaction open across network
    calls.

    `covers_retention` says whether this sweep reached the retention frontier.
    Only one that did may write the watermark: a narrower sweep observed
    nothing about the days it skipped, and recording its last day would assert
    "everything through today has been seen" over days nobody asked about --
    turning the only gap alarm the archive has permanently green.
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
        message = f"{scope.label}/{entity.fundosnet_id}: {error}"
        report.errors.append(message)
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

        if result.complete and covers_retention:
            repo.advance_watermark(entity.fundosnet_id, window.last)
        elif not result.complete:
            report.incomplete_scans += 1
            repo.record_entity_error(
                entity.fundosnet_id,
                f"scan covered {len(result.rows)} of {result.records_filtered} records",
            )

    report.documents_new += new_count

    # Refresh the stored source-side spelling from the newest row. The
    # resolver writes it once, from a probe -- or, for a fund that had filed
    # nothing, from listarFundos text that may never fold-equal a real listing
    # row -- and a rename would otherwise stay unobserved forever. The
    # monitor's gate matches against exactly this field, so keeping it current
    # is what makes the design note's "the sweep refreshes the spelling" true.
    if result.rows:
        observed = max(result.rows, key=lambda row: row.delivery_at).fund_description
        if observed and observed != entity.fnet_fund_description:
            log.info(
                "entity description refreshed from the listing",
                extra={
                    "scope": scope.label,
                    "fundosnet_id": entity.fundosnet_id,
                    "old": entity.fnet_fund_description[:90],
                    "new": observed[:90],
                },
            )
            entity.fnet_fund_description = observed
            report.descriptions_refreshed += 1

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
            "watermark_advanced": result.complete and covers_retention,
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
    retention: RetentionWindow | None = None,
) -> DiscoveryReport:
    """Discover across every entity of every scope.

    A failing entity is recorded and skipped; the rest of the batch continues.
    One misconfigured fund must never cost a day of everyone else's documents.

    `window` is the sweep's own window, which the monitor profile deliberately
    makes narrower than retention. `retention` is the window the archive
    promises to hold; whether the watermark may advance is derived from the two
    of them and never from a profile flag, so the rule cannot drift away from
    the numbers it is about. Omitting it says this sweep *is* the retention
    sweep, which is what every caller before the profiles existed meant.
    """
    report = DiscoveryReport()
    covers_retention = retention is None or window.first <= retention.first

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
                    covers_retention=covers_retention,
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


def run_monitor(
    client: FnetClient,
    repo: ManifestRepo,
    scopes: list[Scope],
    window: RetentionWindow,
    *,
    page_length: int,
    should_stop: object = None,
    retention: RetentionWindow | None = None,
) -> DiscoveryReport:
    """Discover through the listing gate: the frequent profile's variant of `run`.

    Stated deviation from the architecture's "global listing is detective-only"
    rule, formalized in revision 4: the listing gates which per-entity queries
    the monitor spends; it never routes a document. Per fund type in the watch
    list, one newest-first read collects the rows the cursor has not accounted
    for; rows matching a monitored name (folded, exact, within that type) gate
    the normal `discover_entity` call, and only those per-entity results enter
    the manifest. A gate miss costs latency only -- the daily sweep still
    queries every entity unconditionally and remains the completeness guarantee.

    The cursor advances only when both held: the read came back `complete`
    (frontier crossed under validated descending order, or the listing ended
    normally) *and* every per-entity discovery it gated concluded without
    raising. Not advancing self-heals -- the next firing re-reads the same rows
    and retries -- while advancing past a failed gated discovery would silently
    hand the document to the sweep alone, forfeiting exactly the latency the
    monitor exists to buy. Individual malformed rows do not hold the cursor
    back: they are counted as accounted for, the same arithmetic as the
    ascending scan's coverage, because one defective row must not poison the
    cursor forever and the sweep absorbs whatever it named.

    A failed read gates nothing and never falls back to per-entity discovery:
    that would cost one request per entity at the exact moment the source is
    unhealthy, and would keep alive the mechanism this one replaces.

    The signature mirrors `run` so the composition root chooses between them
    and nothing below it knows which profile is running.
    """
    report = DiscoveryReport()
    covers_retention = retention is None or window.first <= retention.first

    for scope in scopes:
        if not scope.resolved:
            log.error(
                "scope has no resolved entities and will be skipped", extra={"scope": scope.label}
            )

    resolved = [scope for scope in scopes if scope.resolved]
    names_by_type = watchlist.monitored_names(resolved)

    for fund_type in watchlist.fund_types(resolved):
        if callable(should_stop) and should_stop():
            log.warning("stopping discovery early on request")
            return report

        cursor = repo.listing_cursor(fund_type)
        try:
            result = scan_newest(
                client,
                first=window.first,
                last=window.last,
                fund_type=fund_type,
                cursor=cursor,
                # Hardcoded like the audit's scan: the cost argument for the
                # gate is sized at the endpoint's ceiling. [source].page_length
                # keeps governing the per-entity queries below.
                page_length=200,
            )
        except (TransientSourceError, WatcherError) as exc:
            report.listing_read_failures += 1
            report.errors.append(f"tipoFundo={fund_type}: newest-first read failed: {exc}")
            log.log(
                getattr(exc, "severity", logging.ERROR),
                "newest-first read failed; the sweep covers this fund type",
                extra={"fund_type": fund_type, "error": str(exc)},
            )
            continue

        # Malformed rows: recorded exactly as the ascending scan records them
        # (partial state, exit code 1), and counted as accounted for -- they
        # never hold the cursor back, because one defective row must not
        # poison it forever. A monitored fund it might have named is absorbed
        # by the sweep.
        report.invalid_rows += len(result.row_errors)
        for error in result.row_errors:
            message = f"tipoFundo={fund_type}: {error}"
            report.errors.append(message)
            log.error(
                "listing row failed validation and was skipped",
                extra={"fund_type": fund_type, "error": str(error)},
            )

        if not result.complete:
            # scan_newest already logged the abort at its proper severity. Only
            # a broken descending contract turns the exit code; a
            # recordsFiltered drift is legitimate re-filing shrinkage and the
            # next firing re-reads from the frozen cursor.
            if result.contract_broken:
                report.listing_read_failures += 1
                report.errors.append(f"tipoFundo={fund_type}: {result.failure}")
            continue

        if cursor is None:
            candidates = list(result.rows)
        else:
            above = [row for row in result.rows if row.delivery_at > cursor]
            # Rows tied at the cursor minute re-appear on every read; only the
            # identities the manifest does not know yet are new. (id, versao)
            # decides, never the timestamp.
            ties = [row for row in result.rows if row.delivery_at == cursor]
            known = repo.known_identities([row.identity for row in ties])
            candidates = above + [row for row in ties if row.identity not in known]

        names = names_by_type.get(fund_type, {})
        gated: dict[int, tuple[Scope, Entity]] = {}
        for row in candidates:
            for scope, entity in names.get(normalize_name(row.fund_description), []):
                # One entity matched by several rows, or by both of its
                # spellings, is queried once.
                gated.setdefault(entity.fundosnet_id, (scope, entity))

        all_gated_succeeded = True
        for scope, entity in gated.values():
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
                    covers_retention=covers_retention,
                )
                report.entities_scanned += 1
            except (TransientSourceError, WatcherError) as exc:
                all_gated_succeeded = False
                report.entities_failed += 1
                message = f"{scope.label}/{entity.fundosnet_id}: {exc}"
                report.errors.append(message)
                log.log(
                    getattr(exc, "severity", logging.ERROR),
                    "gated entity scan failed; the cursor stays put so the next firing retries",
                    extra={
                        "scope": scope.label,
                        "fundosnet_id": entity.fundosnet_id,
                        "error": str(exc),
                    },
                )
                repo.record_entity_error(entity.fundosnet_id, str(exc))

        advanced = all_gated_succeeded and result.newest is not None
        if advanced:
            assert result.newest is not None
            repo.advance_listing_cursor(fund_type, result.newest)
        log.info(
            "newest-first read finished",
            extra={
                "fund_type": fund_type,
                "pages": result.pages,
                "rows": len(result.rows),
                "candidates": len(candidates),
                "gated_entities": len(gated),
                "cursor_advanced": advanced,
            },
        )

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
