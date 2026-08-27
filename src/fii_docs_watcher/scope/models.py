"""The monitored scope and its entities.

The unit of configuration is a *scope*, identified by one reference CNPJ that
the user supplies. A scope holds one or more *entities*, each with its own CNPJ
and its own Fundos.NET id, because since the RCVM 175 adaptation a fund's
documents may be filed by its classes rather than by the fund.

The user is never required to understand that structure: they register one CNPJ
and the robot expands it. In a monoclass fund -- most listed FIIs, and the
overwhelming majority of what this will monitor -- the expansion produces
exactly one entity and the machinery is invisible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from ..text import fold_name
from .cnpj import format_masked, normalize


class ScopeMode(StrEnum):
    """How far a scope reaches when the reference CNPJ names a fund."""

    FUND_AND_CLASSES = "fund_and_classes"
    THIS_ENTITY_ONLY = "this_entity_only"


class ExpansionState(StrEnum):
    """Whether the entity list is believed to be the whole story.

    `PARTIAL` is the graceful-degradation marker: expansion failed or found
    nothing, so the scope runs on the single entity it could resolve. A
    monoclass fund must never be blocked by multiclass machinery.
    """

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNRESOLVED = "unresolved"


@dataclass
class Entity:
    """One queryable thing in Fundos.NET: a fund, or a class of one."""

    cnpj: str
    fundosnet_id: int
    fnet_fund_description: str = ""
    kind: str = "fund_or_class"
    # Which Fundos.NET fund type answers for this entity. Discovered during
    # resolution and cached like the id, because a query sent under the wrong
    # type returns an empty result rather than an error. The default keeps watch
    # lists written before this field was introduced working unchanged.
    fnet_fund_type: int = 1
    validated_at: str | None = None
    # Set once a download's Content-Disposition CNPJ has confirmed this entity.
    # Until then the name -> id resolution is textual and unproven.
    cnpj_confirmed: bool = False

    @property
    def normalized_cnpj(self) -> str | None:
        return normalize(self.cnpj)


@dataclass
class Scope:
    """One monitored fund, as configured plus as resolved."""

    # Authored by the user. The display form is preserved exactly as typed.
    cnpj: str
    mode: ScopeMode = ScopeMode.FUND_AND_CLASSES
    ticker: str | None = None

    # Filled in by the robot: an inspectable cache, overwritten on each sync.
    legal_name: str | None = None
    cvm_code: str | None = None
    cvm_status: str | None = None
    registered_at: str | None = None
    expansion: ExpansionState = ExpansionState.UNRESOLVED
    entities: list[Entity] = field(default_factory=list)

    @property
    def normalized_cnpj(self) -> str | None:
        return normalize(self.cnpj)

    @property
    def resolved(self) -> bool:
        return bool(self.entities)

    @property
    def label(self) -> str:
        """A short, stable name for logs and CLI output."""
        return self.ticker or self.legal_name or format_masked(self.cnpj) or str(self.cnpj)

    def matches(self, query: str) -> bool:
        """Does this scope answer to `query`?

        Deliberately broad, because a person looking for a registered fund will
        reach for whichever handle they remember: the ticker they annotated, a
        word from the name, or some digits of the CNPJ. Names are folded so an
        accent-free search still finds `IMOBILIÁRIO`; the CNPJ is matched on
        bare digits so any punctuation style works.
        """
        needle = query.strip()
        if not needle:
            return True

        digits = re.sub(r"\D", "", needle)
        if digits and not any(character.isalpha() for character in needle):
            haystack_digits = [self.normalized_cnpj or ""]
            haystack_digits += [e.normalized_cnpj or "" for e in self.entities]
            if any(digits in candidate for candidate in haystack_digits):
                return True

        folded = fold_name(needle)
        if not folded:
            return False
        fields = [self.ticker or "", self.legal_name or ""]
        fields += [e.fnet_fund_description for e in self.entities]
        return any(folded in fold_name(field) for field in fields)

    def entity_for_cnpj(self, cnpj: str | None) -> Entity | None:
        """Find the entity a CNPJ belongs to.

        Used to check a downloaded document's Content-Disposition CNPJ. The
        comparison is against the *entities*, never against the scope's own
        reference CNPJ: in a multiclass fund a class CNPJ legitimately differs
        from the fund's, and comparing against the umbrella would report a
        divergence that is not one.
        """
        target = normalize(cnpj)
        if target is None:
            return None
        for entity in self.entities:
            if entity.normalized_cnpj == target:
                return entity
        return None
