"""The shared watch-list index: per fund type, fanning out on collisions."""

from __future__ import annotations

from fii_docs_watcher.pipeline import watchlist
from fii_docs_watcher.scope.models import Entity, Scope


def _scope(cnpj: str, legal_name: str, *entities: Entity) -> Scope:
    return Scope(cnpj=cnpj, legal_name=legal_name, entities=list(entities))


def test_names_are_indexed_under_their_entity_fund_type_only() -> None:
    scope = _scope(
        "08431747000106",
        "Fundo Um",
        Entity(cnpj="08431747000106", fundosnet_id=1, fnet_fund_type=11),
    )
    index = watchlist.monitored_names([scope])
    assert set(index) == {11}
    assert "FUNDO UM" in index[11]
    assert watchlist.fund_types([scope]) == [11]


def test_both_spellings_index_the_entity_and_fold_exactly() -> None:
    entity = Entity(
        cnpj="08431747000106",
        fundosnet_id=1,
        fnet_fund_description="FUNDO UM - FII",
    )
    index = watchlist.monitored_names([_scope("08431747000106", "Fundo Um", entity)])
    names = index[1]
    assert set(names) == {"FUNDO UM", "FUNDO UM FII"}
    # Folded equality, never substring: a longer name sharing the prefix is
    # absent from the index rather than reachable through it.
    assert "FUNDO UM FII II" not in names


def test_a_folded_collision_keeps_every_match() -> None:
    first = _scope(
        "08431747000106", "Fundo Um", Entity(cnpj="08431747000106", fundosnet_id=1)
    )
    second = _scope(
        "99999999000199", "FUNDO-UM", Entity(cnpj="99999999000199", fundosnet_id=2)
    )
    index = watchlist.monitored_names([first, second])
    assert [entity.fundosnet_id for _scope_, entity in index[1]["FUNDO UM"]] == [1, 2]


def test_an_empty_description_never_indexes_a_blank_name() -> None:
    entity = Entity(cnpj="08431747000106", fundosnet_id=1, fnet_fund_description="")
    index = watchlist.monitored_names([_scope("08431747000106", "", entity)])
    assert index == {1: {}} or index[1] == {}


def test_unresolved_scopes_are_listed_separately() -> None:
    unresolved = Scope(cnpj="08431747000106", legal_name="Fundo Um")
    assert watchlist.monitored_names([unresolved]) == {}
    assert watchlist.fund_types([unresolved]) == []
    assert [s.cnpj for s in watchlist.unassigned_names([unresolved])["FUNDO UM"]] == [
        "08431747000106"
    ]
