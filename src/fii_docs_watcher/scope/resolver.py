"""Turning a CNPJ into queryable Fundos.NET entities.

The chain, and why each link exists:

    reference CNPJ (what the user registers)
      -> CVM registry          the listing never returns a CNPJ, so only the
                               registry can say which fund a CNPJ names, which
                               classes belong to it, and which Fundos.NET fund
                               types are worth trying
      -> listarFundos(name)    the Fundos.NET id is opaque and derivable from
                               nothing; it can only be found by text search,
                               and only within the right fund type
      -> confirm with l=1      a name match is not proof; querying the candidate
                               id confirms it exists and captures its exact
                               descricaoFundo
      -> Content-Disposition   later, on the first real download, the CNPJ in
                               the served filename closes the loop (see fetch)

**On the fund type.** It is discovered, not assumed. A name is only listed under
the category it is filed as, and searching the wrong one returns an empty result
rather than an error -- so a wrong guess is indistinguishable from a fund that
does not exist. The registry narrows it to a short ordered list of candidates,
usually one, and the first type that names the entity wins. The answer is stored
on the entity so it costs one resolution, not one per run.

**On using the registry for structure.** The architecture document prefers
expanding classes through `listarFundos` and treats the registry's class file as
a last resort, on the grounds that every external dependency is permanent
maintenance cost. That cost is already paid here: the same archive is required
for the CNPJ-to-name step, because nothing else can perform it. Given that,
joining classes structurally on `ID_Registro_Fundo` is strictly better than
matching class names by substring -- text routing is the failure mode the
per-entity design exists to eliminate, and the registry contains at least one
fund whose classes would collide under a name match. `listarFundos` remains the
only source of the id itself.

**Degradation is graceful by design.** If expansion finds nothing, the scope
runs on the single entity it could resolve and is marked `partial`. A monoclass
fund is never blocked by machinery meant for multiclass ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from ..clock import to_dir_name, today
from ..cvm.registry import RegistryEntity, RegistrySnapshot
from ..errors import ScopeResolutionError, TransientSourceError
from ..fnet import funds as fnet_funds
from ..fnet.client import FnetClient
from ..fnet.funds import FundCandidate
from ..fnet.listing import FUND_TYPE_FII, probe
from ..text import fold_name
from .models import Entity, ExpansionState, Scope, ScopeMode

log = logging.getLogger(__name__)

# How far back to look when confirming that a candidate id is real. Wide, because
# plenty of funds go weeks without filing anything and a short window would make
# a perfectly good id look nonexistent.
CONFIRMATION_LOOKBACK_DAYS = 540

# Defined alongside the registry so that name search can live there without
# importing this module; re-exported here because this is where callers expect it.
normalize_name = fold_name


@dataclass
class ResolvedCandidate:
    """A `listarFundos` hit, plus what confirming it against the listing showed."""

    candidate: FundCandidate
    confirmed: bool
    fnet_description: str
    document_count: int
    fund_type: int = FUND_TYPE_FII

    @property
    def fundosnet_id(self) -> int:
        return self.candidate.fundosnet_id


def confirm_candidate(
    client: FnetClient, candidate: FundCandidate, *, fund_type: int = FUND_TYPE_FII
) -> ResolvedCandidate:
    """Query a candidate id over a wide window to prove it exists.

    One request, via `probe`. A candidate that returns no documents is not
    rejected: quiet funds are common, and rejecting one would block a legitimate
    registration. It is simply left unconfirmed, to be settled by the first
    download's CNPJ check.
    """
    last = today()
    first = last - timedelta(days=CONFIRMATION_LOOKBACK_DAYS)
    log.info(
        "confirming a candidate against the listing (one request; the source can take ~60s)",
        extra={
            "fundosnet_id": candidate.fundosnet_id,
            "fund_type": fund_type,
            "candidate": candidate.text[:70],
        },
    )
    try:
        result = probe(
            client,
            first=first,
            last=last,
            fundosnet_id=candidate.fundosnet_id,
            fund_type=fund_type,
        )
    except TransientSourceError as exc:
        log.warning(
            "could not confirm a candidate id against the listing",
            extra={"fundosnet_id": candidate.fundosnet_id, "error": str(exc)},
        )
        return ResolvedCandidate(candidate, False, candidate.denomination, 0, fund_type)

    description = (
        result.first_row.fund_description if result.first_row else candidate.denomination
    )
    return ResolvedCandidate(
        candidate=candidate,
        confirmed=result.exists,
        fnet_description=description,
        document_count=result.records_filtered,
        fund_type=fund_type,
    )


def find_candidates(
    client: FnetClient, term: str, *, fund_type: int = FUND_TYPE_FII
) -> list[FundCandidate]:
    """Search Fundos.NET by name. Used by the interactive CLI path."""
    return fnet_funds.search(client, term, fund_type=fund_type)


def _shortlist(candidates: list[FundCandidate], target_name: str) -> list[FundCandidate]:
    """Narrow a name search to the plausible ids, preferring an exact match.

    `listarFundos` matches on substring, so a search for one fund routinely
    returns its classes and unrelated funds sharing a word. It also returns
    genuine duplicates: one class name resolves to two different ids.
    """
    wanted = normalize_name(target_name)
    exact = [c for c in candidates if normalize_name(c.denomination) == wanted]
    if exact:
        return exact
    return [
        c
        for c in candidates
        if wanted and (wanted in normalize_name(c.text) or normalize_name(c.text) in wanted)
    ]


def _pick(
    client: FnetClient, target_name: str, fund_types: tuple[int, ...]
) -> ResolvedCandidate | None:
    """Choose the id and the fund type for one entity.

    The type has to be discovered, not assumed: a name is only listed under the
    category it is filed as, and querying the wrong one returns an empty result
    rather than an error. The CVM registry narrows it to a short ordered list of
    candidates -- usually one -- and the first type that names the entity wins.

    Within a type, candidates are ranked rather than taken in order, and every
    finalist is confirmed against the listing before being trusted.
    """
    for fund_type in fund_types or (FUND_TYPE_FII,):
        candidates = find_candidates(client, target_name, fund_type=fund_type)
        shortlist = _shortlist(candidates, target_name)
        if not shortlist:
            continue

        # A handful only; needing more means the name is too generic to resolve
        # unattended, and silently considering only a prefix can select the wrong
        # entity solely because of the source's result order.
        if len(shortlist) > 5:
            log.error(
                "too many Fundos.NET ids matched one name; refusing automatic resolution",
                extra={
                    "target": target_name[:80],
                    "fund_type": fund_type,
                    "candidates": len(shortlist),
                },
            )
            return None

        checked = [
            confirm_candidate(client, candidate, fund_type=fund_type)
            for candidate in shortlist
        ]

        # Prefer a candidate that actually has documents; among equals, the one
        # with the most, since a duplicate registration is typically the empty one.
        checked.sort(key=lambda r: (r.confirmed, r.document_count), reverse=True)
        best = checked[0]

        if len(shortlist) > 1:
            log.info(
                "several ids matched one name; picked the one with documents",
                extra={
                    "target": target_name[:80],
                    "picked": best.fundosnet_id,
                    "fund_type": fund_type,
                    "documents": best.document_count,
                    "others": [
                        c.fundosnet_id
                        for c in shortlist
                        if c.fundosnet_id != best.fundosnet_id
                    ],
                },
            )
        return best

    return None


def _entity_from(resolved: ResolvedCandidate, registry_entity: RegistryEntity) -> Entity:
    return Entity(
        cnpj=registry_entity.cnpj,
        fundosnet_id=resolved.fundosnet_id,
        fnet_fund_description=resolved.fnet_description,
        kind=registry_entity.kind,
        fnet_fund_type=resolved.fund_type,
        validated_at=None,
        cnpj_confirmed=False,
    )


def _resolve_registry_entities(
    client: FnetClient, registry_entities: list[RegistryEntity]
) -> tuple[list[Entity], int]:
    """Resolve each registry entity while isolating only transient source failures."""
    resolved_entities: list[Entity] = []
    failures = 0
    total = len(registry_entities)
    for position, registry_entity in enumerate(registry_entities, start=1):
        try:
            log.info(
                "entity %d/%d: searching Fundos.NET by name",
                position,
                total,
                extra={
                    "entity_cnpj": registry_entity.cnpj,
                    "fund_types": list(registry_entity.candidate_fnet_types),
                },
            )
            best = _pick(
                client, registry_entity.legal_name, registry_entity.candidate_fnet_types
            )
        except TransientSourceError as exc:
            failures += 1
            log.warning(
                "could not resolve an entity to a Fundos.NET id",
                extra={"entity_cnpj": registry_entity.cnpj, "error": str(exc)},
            )
            continue

        if best is None:
            failures += 1
            log.warning(
                "no unambiguous Fundos.NET entry matched an entity's registered name",
                extra={
                    "entity_cnpj": registry_entity.cnpj,
                    "legal_name": registry_entity.legal_name[:90],
                },
            )
            continue

        resolved_entities.append(_entity_from(best, registry_entity))
    return resolved_entities, failures


def _entity_identity(entity: Entity) -> tuple[int, int, str | None]:
    """Identity whose CNPJ confirmation can safely be reused."""
    return entity.fundosnet_id, entity.fnet_fund_type, entity.normalized_cnpj


def _consolidate_entities(
    resolved_entities: list[Entity], previous_entities: list[Entity]
) -> tuple[list[Entity], int]:
    """Deduplicate exact identities and reject one id assigned to different entities."""
    by_id: dict[int, Entity] = {}
    conflicted_ids: set[int] = set()
    for entity in resolved_entities:
        fundosnet_id = entity.fundosnet_id
        prior = by_id.get(fundosnet_id)
        if prior is None:
            by_id[fundosnet_id] = entity
            continue
        if _entity_identity(prior) == _entity_identity(entity):
            continue
        conflicted_ids.add(fundosnet_id)
        log.error(
            "one Fundos.NET id resolved to conflicting entity identities; dropping it",
            extra={
                "fundosnet_id": fundosnet_id,
                "first_cnpj": prior.cnpj,
                "first_fund_type": prior.fnet_fund_type,
                "other_cnpj": entity.cnpj,
                "other_fund_type": entity.fnet_fund_type,
            },
        )

    consolidated = [
        entity for fundosnet_id, entity in by_id.items() if fundosnet_id not in conflicted_ids
    ]
    previous = {_entity_identity(entity): entity for entity in previous_entities}
    for entity in consolidated:
        prior = previous.get(_entity_identity(entity))
        if prior is not None and prior.cnpj_confirmed:
            entity.cnpj_confirmed = True
            entity.validated_at = prior.validated_at
    return consolidated, len(conflicted_ids)


def resolve_scope(
    client: FnetClient,
    snapshot: RegistrySnapshot,
    scope: Scope,
) -> Scope:
    """Resolve a scope into entities, mutating and returning it.

    Raises `ScopeResolutionError` only when nothing at all could be resolved.
    The caller records that and moves on to the next scope.
    """
    cnpj = scope.normalized_cnpj
    if cnpj is None:
        raise ScopeResolutionError(f"scope has an unusable CNPJ: {scope.cnpj!r}")

    anchor, registry_entities = snapshot.expand(cnpj)
    if anchor is None:
        raise ScopeResolutionError(
            f"CNPJ {scope.cnpj} is not a fund or class this robot can monitor: the CVM "
            "registry holds no entry for it in a category Fundos.NET publishes",
            context={"cnpj": cnpj},
        )

    scope.legal_name = anchor.legal_name
    scope.cvm_code = anchor.cvm_code
    scope.cvm_status = anchor.situation

    log.info(
        "resolving scope: %s",
        anchor.legal_name[:70],
        extra={
            "cnpj": scope.cnpj,
            "entities_to_resolve": len(registry_entities),
            "kind": anchor.kind,
        },
    )

    if not anchor.active:
        log.warning(
            "scope's CVM situation is not active; it will still be monitored",
            extra={"scope": scope.label, "cvm_status": anchor.situation},
        )

    # `this_entity_only` means the user asked for exactly what they registered,
    # so the class expansion is skipped entirely.
    if scope.mode is ScopeMode.THIS_ENTITY_ONLY:
        target = snapshot.lookup(cnpj) or anchor
        registry_entities = [target]

    resolved_entities, failures = _resolve_registry_entities(client, registry_entities)
    consolidated, conflicts = _consolidate_entities(resolved_entities, scope.entities)
    failures += conflicts

    if not consolidated:
        raise ScopeResolutionError(
            f"no entity of scope {scope.label} could be resolved to a Fundos.NET id",
            context={"cnpj": cnpj, "attempted": len(registry_entities)},
        )

    scope.entities = consolidated
    scope.expansion = (
        ExpansionState.PARTIAL if failures else ExpansionState.COMPLETE
    )
    if scope.registered_at is None:
        scope.registered_at = to_dir_name(today())

    log.info(
        "scope resolved",
        extra={
            "scope": scope.label,
            "entities": len(scope.entities),
            "expansion": scope.expansion.value,
        },
    )
    return scope
