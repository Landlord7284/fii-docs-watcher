"""`listarFundos`: resolving a legal name to Fundos.NET's internal id.

There is no formula that derives this id from a CNPJ or a CVM code -- it is an
opaque internal identifier, reachable only by text search, so it is resolved
once and cached in the YAML.

Two behaviours that the architecture document does not mention, both verified:

**It pages at 20 results.** The response carries `more: true` when further
pages exist, and `page` selects them. A fund whose name shares a prefix with
many others is easy to miss by reading only the first page.

**The same name can map to several ids.** `CLASSE A DE COTAS DO VBI ULIVING
MULTICLASSE` resolves to both 1054 and 20524. Candidates are therefore never
auto-selected on a name match alone; each is confirmed against the document
search before it is trusted.

Matching is substring-based, which is exactly why a fund's own denomination
also surfaces its classes: `URBANITY CORPORATE` returns the fund (25256) plus
`CLASSE A DO URBANITY CORPORATE` (25257) and `CLASSE B` (25258).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..errors import SourceContractError
from .client import FnetClient

log = logging.getLogger(__name__)

LIST_FUNDS_PATH = "listarFundos"
FUND_TYPE_FII = 1

# Observed page size. Used only as a loop guard; `more` is what actually drives
# pagination, so a change at the source degrades to extra requests, not to loss.
PAGE_SIZE = 20
MAX_PAGES = 50


@dataclass(frozen=True)
class FundCandidate:
    """One `{id, text}` entry from `listarFundos`."""

    fundosnet_id: int
    text: str

    @property
    def denomination(self) -> str:
        """The legal name, with the display alias stripped when there is one.

        `text` comes in two shapes: bare (`URBANITY CORPORATE FUNDO ...`) or
        alias-prefixed (`FII BRIO III - BRIO REAL ESTATE III - FUNDO ...`). The
        alias is an internal nickname, never the B3 ticker, and the separator
        also occurs inside legal names -- so the prefix is only stripped when it
        actually looks like an alias: short, and starting with `FII `.
        """
        text = self.text.strip()
        prefix, separator, rest = text.partition(" - ")
        if separator and prefix.startswith("FII ") and len(prefix) <= 20:
            return rest.strip()
        return text


def _parse(payload: Any) -> tuple[list[FundCandidate], bool]:
    if not isinstance(payload, dict) or "results" not in payload:
        raise SourceContractError(
            "listarFundos response has no 'results' array",
            context={"keys": sorted(payload) if isinstance(payload, dict) else type(payload)},
        )
    candidates: list[FundCandidate] = []
    for entry in payload.get("results") or []:
        if not isinstance(entry, dict) or entry.get("id") is None:
            continue
        try:
            fundosnet_id = int(entry["id"])
        except (TypeError, ValueError):
            log.warning("skipping listarFundos entry with a non-integer id", extra={"entry": entry})
            continue
        candidates.append(FundCandidate(fundosnet_id, str(entry.get("text") or "").strip()))
    return candidates, bool(payload.get("more"))


def search(
    client: FnetClient, term: str, *, fund_type: int = FUND_TYPE_FII
) -> list[FundCandidate]:
    """Find every entity whose name contains `term`, following all pages.

    Results are deduplicated by id while preserving order, because the same id
    can legitimately appear on more than one page when the underlying set shifts
    mid-scan.

    `fund_type` selects the catalogue to search. A name is only ever found under
    the type it is filed as, so searching the wrong one returns nothing at all
    rather than an error -- which is why the caller tries the candidates the CVM
    registry suggests instead of assuming one.
    """
    term = term.strip()
    if not term:
        return []

    found: dict[int, FundCandidate] = {}
    for page in range(1, MAX_PAGES + 1):
        payload = client.get(
            LIST_FUNDS_PATH,
            {
                "term": term,
                "page": page,
                "idTipoFundo": fund_type,
                "idAdm": 0,
                "paraCerts": "false",
            },
        ).json()
        candidates, has_more = _parse(payload)
        for candidate in candidates:
            found.setdefault(candidate.fundosnet_id, candidate)
        if not has_more or not candidates:
            break
    else:
        log.warning(
            "listarFundos still reported more pages at the page limit; results may be truncated",
            extra={"term": term, "pages": MAX_PAGES, "found": len(found)},
        )

    log.debug(
        "listarFundos resolved",
        extra={"term": term, "fund_type": fund_type, "candidates": len(found)},
    )
    return list(found.values())
