"""Document listing and its pagination.

**The sort parameter is not optional.** Paging a full day at l=50 with no sort
returned 217 rows for a `recordsFiltered` of 217 -- while containing only 175
distinct ids. 42 rows (19%) were silently skipped and 42 others served twice,
so the row-count check the spec proposes matched perfectly while a fifth of the
day went missing. The underlying cause is an unstable sort with no tiebreaker:
identical requests are deterministic, but the ordering is not consistent
*across offsets*.

Measured, same window, same page size:

    (no sort)                  217 collected / 175 distinct  -- 42 lost
    o[0][id]=asc               ignored, same loss
    o[0][0]=asc                ignored, same loss
    o[0][dataEntrega]=asc      217 collected / 217 distinct  -- clean

So every request sends `o[0][dataEntrega]=asc`, and coverage is asserted on
*distinct identities* rather than on the row count, which is the only check
that would have caught this.

The one exception is `scan_newest`, the monitor profile's newest-first read.
Descending on `dataEntrega` was measured clean on 2026-08-27 (333/333 rows over
a four-day window, order verified non-increasing, identical to the ascending
set), but an early-stopped read cannot assert identities against
`recordsFiltered` -- so it validates the ordering itself on every read and
aborts on any violation instead. Every paginating full-coverage scan still
sends `asc`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..clock import to_wire_date
from ..errors import SourceContractError
from .client import FnetClient
from .schema import DocumentRow, parse_rows

log = logging.getLogger(__name__)

SEARCH_PATH = "pesquisarGerenciadorDocumentosDados"

# The only ordering safe for full-coverage pagination. Changing it re-opens
# silent row loss. `scan_newest` sends the descending counterpart and validates
# the order it receives, because it cannot fall back on the coverage check.
STABLE_SORT = {"o[0][dataEntrega]": "asc"}
NEWEST_FIRST_SORT = {"o[0][dataEntrega]": "desc"}

# `idTipoFundo` / `tipoFundo` = 1 selects real-estate funds. Other categories
# answer under their own id and return nothing under this one, so the type
# travels with the entity rather than being assumed; see `cvm.registry`.
FUND_TYPE_FII = 1

# A scan re-runs at most this many times before the shortfall is reported.
MAX_SCAN_ATTEMPTS = 3


@dataclass
class ProbeResult:
    """The answer to "is this id real, and what is it called?" in one request."""

    records_filtered: int = 0
    first_row: DocumentRow | None = None

    @property
    def exists(self) -> bool:
        return self.records_filtered > 0


@dataclass
class ScanResult:
    """Everything one entity-window scan learned, including what went wrong."""

    rows: list[DocumentRow] = field(default_factory=list)
    records_filtered: int = 0
    pages: int = 0
    row_errors: list[SourceContractError] = field(default_factory=list)
    attempts: int = 1

    @property
    def complete(self) -> bool:
        """Did we account for every row the source said existed?

        Rows that failed schema validation still count as accounted for: they
        were seen and reported, not skipped by pagination.
        """
        return len(self.rows) + len(self.row_errors) >= self.records_filtered


def _search_params(
    *,
    start: int,
    length: int,
    first: date,
    last: date,
    fundosnet_id: int | None,
    fund_type: int,
    descending: bool = False,
) -> dict[str, Any]:
    """Build one search request.

    `idFundo` is omitted entirely for a global scan: `idFundo=0` is not "all
    funds", it is a nonexistent id and returns nothing. The `cnpj` filter is
    accepted and then ignored by this endpoint, so it is never sent.
    """
    params: dict[str, Any] = {
        "d": 1,
        "s": start,
        "l": length,
        "tipoFundo": fund_type,
        "dataInicial": to_wire_date(first),
        "dataFinal": to_wire_date(last),
        "idCategoriaDocumento": 0,
        "idTipoDocumento": 0,
        "idEspecieDocumento": 0,
        "isSession": "false",
        **(NEWEST_FIRST_SORT if descending else STABLE_SORT),
    }
    if fundosnet_id is not None:
        params["idFundo"] = fundosnet_id
    return params


def _parse_search_payload(payload: Any) -> tuple[list[Any], int]:
    """Validate the page envelope shared by scans and one-request probes."""
    if not isinstance(payload, dict) or "data" not in payload:
        raise SourceContractError(
            "search response has no 'data' array",
            context={"keys": sorted(payload) if isinstance(payload, dict) else type(payload)},
        )
    batch = payload["data"]
    if not isinstance(batch, list):
        raise SourceContractError(
            "search response 'data' is not an array",
            context={"type": type(batch).__name__},
        )
    if "recordsFiltered" not in payload:
        raise SourceContractError("search response has no 'recordsFiltered' count")
    try:
        total = int(payload["recordsFiltered"])
    except (TypeError, ValueError) as exc:
        raise SourceContractError(
            f"recordsFiltered is not an integer: {payload['recordsFiltered']!r}"
        ) from exc
    if total < 0:
        raise SourceContractError(f"recordsFiltered is negative: {total}")
    return batch, total


def _pages(
    client: FnetClient,
    *,
    first: date,
    last: date,
    fundosnet_id: int | None,
    page_length: int,
    fund_type: int,
) -> Iterator[tuple[list[dict[str, Any]], int]]:
    """Yield `(raw rows, recordsFiltered)` page by page until the set is covered."""
    start = 0
    seen = 0
    while True:
        payload = client.get(
            SEARCH_PATH,
            _search_params(
                start=start,
                length=page_length,
                first=first,
                last=last,
                fundosnet_id=fundosnet_id,
                fund_type=fund_type,
            ),
        ).json()
        batch, total = _parse_search_payload(payload)

        yield batch, total
        seen += len(batch)
        if not batch or seen >= total or len(batch) < page_length:
            return
        start += page_length


@dataclass
class NewestReadResult:
    """What one newest-first global read observed, and whether to trust it.

    `complete` is the only field a caller may act on for state: it is true only
    when the cursor frontier was crossed under validated descending order, or
    the listing ended normally. A read that is not `complete` carries real rows,
    but nothing derived from it -- least of all a cursor -- may advance.
    """

    rows: list[DocumentRow] = field(default_factory=list)
    newest: datetime | None = None
    records_filtered: int = 0
    pages: int = 0
    row_errors: list[SourceContractError] = field(default_factory=list)
    complete: bool = False
    failure: str | None = None
    # True when the descending contract itself broke (order violation or a
    # repeated identity): the source changed under us, which is CRITICAL. False
    # for a `recordsFiltered` drift, which a re-filing causes legitimately.
    contract_broken: bool = False


def scan_newest(
    client: FnetClient,
    *,
    first: date,
    last: date,
    fund_type: int,
    cursor: datetime | None,
    page_length: int = 200,
) -> NewestReadResult:
    """Read the global listing newest-first, stopping once `cursor` is reached.

    This is the monitor profile's discovery gate and nothing else: rows are
    matched against the watch list to decide which per-entity queries to spend;
    no row read here ever routes a document into the archive.

    The stop rule: rows at or above the cursor instant are kept; the read ends
    at the first row *strictly below* it. Rows tied at the cursor minute are
    kept and paged past, so a tie group is never split -- `dataEntrega` resolves
    to the minute and several documents can share the boundary instant.
    Deduplication is on `(id, versao)`, never on the timestamp.

    An early stop forfeits the identities-vs-`recordsFiltered` coverage check
    that caught silent row loss in the ascending scan, so order validation is
    the substitute, enforced on every read: `dataEntrega` must be non-increasing
    within and across pages, no identity may repeat, and `recordsFiltered` must
    hold still across the pages of one read. Any violation aborts the read with
    `complete` false. There is no retry loop: an aborted or incomplete read
    costs latency only -- the caller keeps its cursor frozen, the next firing
    re-reads, and the daily sweep remains the completeness guarantee.
    """
    result = NewestReadResult()
    seen_identities: set[tuple[int, int]] = set()
    error_fingerprints: set[str] = set()
    previous: datetime | None = None
    seen_raw = 0
    start = 0

    while True:
        payload = client.get(
            SEARCH_PATH,
            _search_params(
                start=start,
                length=page_length,
                first=first,
                last=last,
                fundosnet_id=None,
                fund_type=fund_type,
                descending=True,
            ),
        ).json()
        batch, total = _parse_search_payload(payload)
        result.pages += 1

        if result.pages == 1:
            result.records_filtered = total
        elif total != result.records_filtered:
            # A re-filing removes the replaced version from the listing, so the
            # total can legitimately shrink mid-read. Rows may have shifted
            # between pages, so nothing about this read is trustworthy -- but
            # nothing about the source is broken either.
            result.failure = (
                f"recordsFiltered changed mid-read ({result.records_filtered} -> {total})"
            )
            log.warning(
                "newest-first read aborted; will re-read from the frozen cursor",
                extra={"fund_type": fund_type, "failure": result.failure},
            )
            return result

        rows, row_errors = parse_rows(batch)
        for error in row_errors:
            fingerprint = str(error.context.get("row_fingerprint", str(error)))
            if fingerprint not in error_fingerprints:
                error_fingerprints.add(fingerprint)
                result.row_errors.append(error)

        crossed = False
        for row in rows:
            if previous is not None and row.delivery_at > previous:
                result.failure = (
                    f"descending order violated: {row.delivery_at.isoformat()} after "
                    f"{previous.isoformat()} (document {row.document_id})"
                )
            elif row.identity in seen_identities:
                result.failure = (
                    f"identity repeated across the read: {row.identity} -- "
                    "the signature of unstable pagination"
                )
            if result.failure is not None:
                result.contract_broken = True
                log.critical(
                    "newest-first read violated the descending contract; "
                    "re-measure the source (pytest -m live) before trusting it",
                    extra={"fund_type": fund_type, "failure": result.failure},
                )
                return result
            previous = row.delivery_at
            seen_identities.add(row.identity)
            if result.newest is None or row.delivery_at > result.newest:
                result.newest = row.delivery_at
            if cursor is not None and row.delivery_at < cursor:
                crossed = True
            else:
                result.rows.append(row)

        seen_raw += len(batch)
        if crossed:
            result.complete = True
            return result
        if not batch or len(batch) < page_length or seen_raw >= result.records_filtered:
            # The listing ended normally: everything above the cursor -- or the
            # whole window, when there was no cursor -- has been seen.
            result.complete = True
            return result
        start += page_length


def probe(
    client: FnetClient,
    *,
    first: date,
    last: date,
    fundosnet_id: int,
    fund_type: int = FUND_TYPE_FII,
) -> ProbeResult:
    """Confirm an entity id is real, in exactly one request.

    Deliberately not `scan(..., page_length=1)`. A scan paginates until the
    whole window is covered, so at a page length of one it issues one request
    per document -- 74 of them for a fund like KINEA RENDA, each able to stall
    for a minute, and then retries the lot if coverage falls short. Resolving a
    single fund could take an hour.

    Confirmation needs neither completeness nor coverage: `recordsFiltered`
    proves the id exists, and the first row carries the exact `descricaoFundo`
    to store. One page of one row answers both.
    """
    payload = client.get(
        SEARCH_PATH,
        _search_params(
            start=0,
            length=1,
            first=first,
            last=last,
            fundosnet_id=fundosnet_id,
            fund_type=fund_type,
        ),
    ).json()
    batch, total = _parse_search_payload(payload)
    rows, _errors = parse_rows(batch)
    return ProbeResult(records_filtered=total, first_row=rows[0] if rows else None)


def scan(
    client: FnetClient,
    *,
    first: date,
    last: date,
    fundosnet_id: int | None = None,
    page_length: int = 200,
    fund_type: int = FUND_TYPE_FII,
) -> ScanResult:
    """Scan `[first, last]` by delivery date, optionally narrowed to one entity.

    Both ends of the interval are inclusive, and the filter applies to
    `dataEntrega` rather than `dataReferencia` -- a document can carry a
    reference date well outside the window it was delivered in.

    Retries the whole scan when distinct-identity coverage falls short of
    `recordsFiltered`, since a shortfall means pagination skipped rows rather
    than that the rows do not exist. If it still falls short, the partial result
    is returned with `complete` false -- callers must check it before treating
    the window as fully scanned.
    """
    target = f"idFundo={fundosnet_id}" if fundosnet_id is not None else "global"
    label = f"{target} tipoFundo={fund_type}"
    last_result: ScanResult | None = None

    for attempt in range(1, MAX_SCAN_ATTEMPTS + 1):
        by_identity: dict[tuple[int, int], DocumentRow] = {}
        errors: list[SourceContractError] = []
        error_fingerprints: set[str] = set()
        records_filtered = 0
        pages = 0

        for batch, total in _pages(
            client,
            first=first,
            last=last,
            fundosnet_id=fundosnet_id,
            page_length=page_length,
            fund_type=fund_type,
        ):
            pages += 1
            records_filtered = max(records_filtered, total)
            rows, row_errors = parse_rows(batch)
            for error in row_errors:
                fingerprint = str(error.context.get("row_fingerprint", str(error)))
                if fingerprint not in error_fingerprints:
                    error_fingerprints.add(fingerprint)
                    errors.append(error)
            for row in rows:
                # Deduplicate by publication identity. The source repeats rows
                # across pages when ordering drifts; the same document arriving
                # twice is not new information.
                by_identity[row.identity] = row

        result = ScanResult(
            rows=list(by_identity.values()),
            records_filtered=records_filtered,
            pages=pages,
            row_errors=errors,
            attempts=attempt,
        )
        last_result = result

        if result.complete:
            if attempt > 1:
                log.info(
                    "scan recovered after re-scan",
                    extra={"entity": label, "attempts": attempt, "rows": len(result.rows)},
                )
            return result

        log.warning(
            "scan coverage short of recordsFiltered; re-scanning",
            extra={
                "entity": label,
                "distinct": len(result.rows),
                "invalid": len(result.row_errors),
                "records_filtered": result.records_filtered,
                "attempt": attempt,
            },
        )

    # Out of attempts. The rows we did collect are real documents and are worth
    # keeping, so the incomplete result is returned rather than raised: the
    # caller persists what was found but must not advance the watermark, since
    # an interrupted scan proves nothing about the rest of the window.
    assert last_result is not None
    log.warning(
        "scan remained incomplete; documents will be recorded but the watermark will not advance",
        extra={
            "entity": label,
            "distinct": len(last_result.rows),
            "records_filtered": last_result.records_filtered,
            "attempts": MAX_SCAN_ATTEMPTS,
        },
    )
    return last_result
