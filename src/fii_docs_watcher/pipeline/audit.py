"""The global-listing cross-check: detective, never corrective.

Once per configured interval the robot scans the global listing for today and
asks whether any document whose `descricaoFundo` matches a monitored scope was
*not* picked up by the per-entity queries. A hit suggests one of three things: a
new class was created under a monitored fund, a stored `id_fundosnet` has gone
stale, or the source changed behaviour.

**It never routes a document.** Matching by name is exactly the silent failure
mode the per-entity design exists to eliminate -- it breaks on renames, on name
collisions between entities, and on classes carrying their own names. Using it
to file a document would reintroduce that failure through the back door. So this
raises an alert asking a human to revalidate the scope, and nothing else.

A failure here is not a failure of the job: the archive is already complete
without it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..clock import today
from ..config import AuditConfig
from ..errors import TransientSourceError, WatcherError
from ..fnet.client import FnetClient
from ..fnet.listing import FUND_TYPE_FII, scan
from ..fnet.schema import DocumentRow
from ..manifest.repo import ManifestRepo
from ..scope.models import Scope
from ..scope.resolver import normalize_name

log = logging.getLogger(__name__)


@dataclass
class AuditReport:
    ran: bool = False
    documents_examined: int = 0
    unmatched: list[str] = field(default_factory=list)
    error: str | None = None


def should_run(config: AuditConfig, repo: ManifestRepo, reference: date | None = None) -> bool:
    """Decide whether the audit is due.

    Weekly is anchored to Monday rather than to a stored counter, so the
    behaviour is the same whether the robot ran yesterday or has been off for a
    fortnight, and it needs no extra state.
    """
    del repo  # Cadence is calendar-driven; no manifest state is consulted.
    if config.frequency == "never":
        return False
    if config.frequency == "daily":
        return True
    return (reference or today()).weekday() == 0


def run(
    client: FnetClient,
    repo: ManifestRepo,
    scopes: list[Scope],
    config: AuditConfig,
    *,
    reference: date | None = None,
) -> AuditReport:
    """Scan the global listing and report documents the per-entity pass missed."""
    report = AuditReport()
    if not should_run(config, repo, reference):
        return report

    day = reference or today()
    report.ran = True

    # Names of every monitored entity, folded for comparison. Both the scope's
    # registered legal name and the exact description Fundos.NET returns are
    # included, because the two spellings diverge routinely.
    monitored: dict[str, Scope] = {}
    known_ids: set[int] = set()
    fund_types: dict[int, None] = {}
    for scope in scopes:
        for name in (scope.legal_name, *(e.fnet_fund_description for e in scope.entities)):
            folded = normalize_name(name or "")
            if folded:
                monitored.setdefault(folded, scope)
        known_ids.update(e.fundosnet_id for e in scope.entities)
        # The global listing is per fund type, so a watch list spanning
        # categories needs one scan each. Only the types actually monitored are
        # scanned: an all-FII watch list costs exactly one request-set, as before.
        for entity in scope.entities:
            fund_types.setdefault(entity.fnet_fund_type, None)

    if not monitored:
        return report

    rows: list[DocumentRow] = []
    for fund_type in fund_types or {FUND_TYPE_FII: None}:
        try:
            # Yesterday and today: a document delivered late in the day can be
            # missed by a run that happened before it landed.
            result = scan(
                client,
                first=day - timedelta(days=1),
                last=day,
                page_length=200,
                fund_type=fund_type,
            )
        except (TransientSourceError, WatcherError) as exc:
            report.error = str(exc)
            log.warning(
                "a global audit scan failed; this does not affect the archive",
                extra={"error": str(exc), "fund_type": fund_type},
            )
            continue
        rows.extend(result.rows)

    report.documents_examined = len(rows)
    captured = {
        identity
        for fundosnet_id in known_ids
        for identity in repo.known_identities_for_entity(fundosnet_id)
    }

    for row in rows:
        scope = monitored.get(normalize_name(row.fund_description))
        if scope is None or row.identity in captured:
            continue
        message = (
            f"document {row.document_id} v{row.version} names {row.fund_description!r}, which "
            f"matches monitored scope {scope.label}, but no per-entity query captured it. "
            "Revalidate the scope: a new class may exist, or a stored fundosnet_id may be stale."
        )
        report.unmatched.append(message)
        log.error(
            "global audit found a document that per-entity discovery did not capture",
            extra={
                "document_id": row.document_id,
                "version": row.version,
                "scope": scope.label,
                "fund_description": row.fund_description[:90],
            },
        )

    log.info(
        "global audit finished",
        extra={
            "examined": report.documents_examined,
            "unmatched": len(report.unmatched),
            "scopes": len(scopes),
            "fund_types": list(fund_types),
        },
    )
    return report
