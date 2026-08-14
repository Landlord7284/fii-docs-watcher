"""CVM registry indexing: expansion, name search, and the duplicates in real data."""

from __future__ import annotations

import pytest

from fii_docs_watcher.cvm.registry import (
    RegisteredClass,
    RegisteredFund,
    RegistrySnapshot,
    fold_name,
)


def _fund(registry_id: str, cnpj: str, name: str, situation: str = "Em Funcionamento Normal"):
    return RegisteredFund(
        registry_id=registry_id,
        cnpj=cnpj,
        cvm_code="1",
        legal_name=name,
        situation=situation,
        fund_type="FII",
    )


def _klass(
    registry_id: str,
    fund_registry_id: str,
    cnpj: str,
    name: str,
    situation: str = "Em Funcionamento Normal",
):
    return RegisteredClass(
        registry_id=registry_id,
        fund_registry_id=fund_registry_id,
        cnpj=cnpj,
        cvm_code="1",
        legal_name=name,
        situation=situation,
        class_type="Classes de Cotas de Fundos FII",
    )


@pytest.fixture
def snapshot() -> RegistrySnapshot:
    funds = [
        # Monoclass: fund and its single class share the CNPJ, the common shape.
        _fund("1", "08431747000106", "HEDGE BRASIL SHOPPING FII"),
        # Genuinely multiclass, each class with its own CNPJ.
        _fund("2", "59849627000164", "REAL ESTATE TRANCOSO II FII"),
        # No class rows at all: not yet adapted to RCVM 175.
        _fund("3", "12005956000165", "KINEA RENDA IMOBILIÁRIA FII"),
        _fund("4", "99999999000199", "LIQUIDATED FUND FII", situation="Cancelado"),
    ]
    classes = [
        _klass("10", "1", "08431747000106", "HEDGE BRASIL SHOPPING FII"),
        _klass("20", "2", "59890241000104", "REAL ESTATE TRANCOSO II FII - CLASSE A"),
        _klass("21", "2", "59891323000165", "REAL ESTATE TRANCOSO II FII - CLASSE B"),
        _klass("22", "2", "11111111000191", "REAL ESTATE TRANCOSO II FII - CLASSE C",
               situation="Cancelado"),
    ]
    return RegistrySnapshot(funds, classes, fetched_at="2026-08-14T00:00:00-03:00")


class TestExpansion:
    def test_a_monoclass_fund_collapses_to_one_entity(self, snapshot) -> None:
        # The fund and its only class share a CNPJ; listing both would query the
        # same entity twice.
        anchor, entities = snapshot.expand("08431747000106")
        assert anchor is not None
        assert len(entities) == 1
        assert entities[0].kind == "class"

    def test_a_fund_cnpj_yields_the_fund_and_its_active_classes(self, snapshot) -> None:
        _anchor, entities = snapshot.expand("59849627000164")
        cnpjs = {e.cnpj for e in entities}
        assert cnpjs == {"59849627000164", "59890241000104", "59891323000165"}
        # The cancelled class is excluded.
        assert "11111111000191" not in cnpjs

    def test_a_class_cnpj_yields_only_that_class(self, snapshot) -> None:
        # Registering a class is an explicit request to monitor just that one.
        anchor, entities = snapshot.expand("59890241000104")
        assert anchor is not None and anchor.kind == "class"
        assert [e.cnpj for e in entities] == ["59890241000104"]

    def test_a_fund_with_no_classes_still_resolves_to_itself(self, snapshot) -> None:
        # 694 of the real registry's FII funds are in this state; returning
        # nothing would leave them unmonitorable.
        _anchor, entities = snapshot.expand("12005956000165")
        assert len(entities) == 1
        assert entities[0].kind == "fund"

    def test_duplicate_class_registrations_are_collapsed(self) -> None:
        # Real data: one fund's classes are registered twice under the same CNPJ
        # and the same name. Left in, the robot would archive it twice.
        funds = [_fund("1", "38498758000174", "IC LOTEAMENTOS E RECEBÍVEIS FII")]
        classes = [
            _klass("34940", "1", "38498758000174", "IC LOTEAMENTOS E RECEBÍVEIS FII"),
            _klass("34941", "1", "38498758000174", "IC LOTEAMENTOS E RECEBÍVEIS FII"),
        ]
        _anchor, entities = RegistrySnapshot(funds, classes, "x").expand("38498758000174")
        assert len(entities) == 1

    def test_an_unknown_cnpj_resolves_to_nothing(self, snapshot) -> None:
        anchor, entities = snapshot.expand("00000000000191")
        assert anchor is None
        assert entities == []

    def test_a_cancelled_fund_still_resolves_but_is_marked_inactive(self, snapshot) -> None:
        # Monitoring continues; the situation is surfaced rather than enforced.
        anchor, _entities = snapshot.expand("99999999000199")
        assert anchor is not None
        assert not anchor.active


class TestNameSearch:
    def test_finds_a_fund_by_a_fragment_of_its_name(self, snapshot) -> None:
        matches = snapshot.search_by_name("kinea renda")
        assert [m.cnpj for m in matches] == ["12005956000165"]

    def test_search_ignores_accents_and_case(self, snapshot) -> None:
        assert snapshot.search_by_name("IMOBILIARIA")
        assert snapshot.search_by_name("imobiliária")

    def test_funds_are_listed_before_classes(self, snapshot) -> None:
        matches = snapshot.search_by_name("TRANCOSO")
        assert matches[0].kind == "fund"

    def test_a_monoclass_class_is_not_listed_twice(self, snapshot) -> None:
        matches = snapshot.search_by_name("HEDGE BRASIL")
        assert len(matches) == 1

    def test_inactive_entities_sort_last(self, snapshot) -> None:
        matches = snapshot.search_by_name("FII")
        assert matches[-1].active is False

    def test_an_empty_term_matches_nothing(self, snapshot) -> None:
        assert snapshot.search_by_name("") == []
        assert snapshot.search_by_name("   ") == []


class TestFoldName:
    def test_folds_accents_case_punctuation_and_runs_of_space(self) -> None:
        assert fold_name("Hedge  Brasil - Imobiliário") == "HEDGE BRASIL IMOBILIARIO"

    def test_two_spellings_of_one_name_fold_together(self) -> None:
        assert fold_name("REAL ESTATE - FII") == fold_name("Real Estate   FII")
