"""The watch list, indexed the way the global listing asks its questions.

Both consumers of the global listing -- the detective audit and the monitor's
discovery gate -- need the same two views of the configured scopes: which fund
types have to be scanned at all, and which folded fund names belong to whom.
Building them here keeps the two from drifting apart, and pins two properties
that are easy to lose in an ad-hoc dict:

- **The index is per fund type.** The listing is scanned one `tipoFundo` at a
  time, and a name is only meaningful within the type its entity answers under.
  A flat index would let a type-1 row match a homonymous type-11 entity.
- **A folded name maps to every match, not the first.** Two monitored funds can
  legitimately fold to one name; keeping only the first silently drops the
  other from whatever the caller does with the hit.
"""

from __future__ import annotations

from ..scope.models import Entity, Scope
from ..scope.resolver import normalize_name


def fund_types(scopes: list[Scope]) -> list[int]:
    """Distinct `fnet_fund_type` across all entities, in first-seen order."""
    seen: dict[int, None] = {}
    for scope in scopes:
        for entity in scope.entities:
            seen.setdefault(entity.fnet_fund_type, None)
    return list(seen)


def monitored_names(scopes: list[Scope]) -> dict[int, dict[str, list[tuple[Scope, Entity]]]]:
    """fund type -> folded name -> every (scope, entity) answering to it.

    A scope's registered legal name and each entity's exact Fundos.NET spelling
    (`fnet_fund_description`) both index the entity, because the two diverge
    routinely. Empty spellings are skipped -- an entity that has not learned
    its description yet must not match every blank-named row.

    Only entities appear: an unresolved scope has nothing queryable, so it has
    no fund type to file its name under. See `unassigned_names` for those.
    """
    index: dict[int, dict[str, list[tuple[Scope, Entity]]]] = {}
    for scope in scopes:
        for entity in scope.entities:
            names = index.setdefault(entity.fnet_fund_type, {})
            for name in (scope.legal_name, entity.fnet_fund_description):
                folded = normalize_name(name or "")
                if folded:
                    matches = names.setdefault(folded, [])
                    if (scope, entity) not in matches:
                        matches.append((scope, entity))
    return index


def unassigned_names(scopes: list[Scope]) -> dict[str, list[Scope]]:
    """Folded legal names of scopes with no entities at all.

    A scope that never resolved has no fund type to be indexed under, but a
    listing row matching its name is still worth an audit alert: it means the
    fund is publishing while the robot cannot query it.
    """
    names: dict[str, list[Scope]] = {}
    for scope in scopes:
        if scope.entities:
            continue
        folded = normalize_name(scope.legal_name or "")
        if folded:
            matches = names.setdefault(folded, [])
            if scope not in matches:
                matches.append(scope)
    return names
